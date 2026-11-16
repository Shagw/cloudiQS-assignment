import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from pydantic import ValidationError
from strands.models import Model

from risk_monitor.agent import run_risk_monitor
from risk_monitor.webhook import WebhookClient, sign

SAMPLE = json.loads((Path(__file__).parent.parent / "examples" / "transactions.json").read_text())


class ScriptedModel(Model):
    """Offline stand-in for Bedrock: replays a fixed sequence of assistant turns."""

    def __init__(self, turns: list[list[dict]]):
        self.turns = iter(turns)
        self.requests: list[list] = []

    def update_config(self, **kwargs: Any) -> None: ...

    def get_config(self) -> dict:
        return {}

    def structured_output(self, *args, **kwargs):
        raise NotImplementedError

    async def stream(self, messages, tool_specs=None, system_prompt=None, **kwargs):
        self.requests.append(list(messages))
        blocks = next(self.turns)
        yield {"messageStart": {"role": "assistant"}}
        for b in blocks:
            if "text" in b:
                yield {"contentBlockStart": {"start": {}}}
                yield {"contentBlockDelta": {"delta": {"text": b["text"]}}}
            else:
                yield {"contentBlockStart": {"start": {"toolUse": {"toolUseId": b["id"], "name": b["name"]}}}}
                yield {"contentBlockDelta": {"delta": {"toolUse": {"input": json.dumps(b["input"])}}}}
            yield {"contentBlockStop": {}}
        stop = "tool_use" if any("name" in b for b in blocks) else "end_turn"
        yield {"messageStop": {"stopReason": stop}}


def recording_webhook(status: int = 204):
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(status)

    client = WebhookClient(url="https://risk.example.com/hook", secret="s3cret", transport=httpx.MockTransport(handler))
    return client, calls


def test_end_to_end_flags_and_notifies():
    model = ScriptedModel(
        [
            [{"id": "1", "name": "get_customer_profile", "input": {"customer_id": "c-100"}}],
            [
                {
                    "id": "2",
                    "name": "notify_risk_team",
                    "input": {"transaction_id": "t004", "risk_level": "high", "reasons": ["100x baseline spend at 3am"]},
                }
            ],
            [{"text": "Reviewed 12 transactions; flagged t004."}],
        ]
    )
    webhook, calls = recording_webhook()
    report = run_risk_monitor(SAMPLE, model=model, webhook=webhook)

    assert report.transactions_reviewed == 12
    assert report.flagged_count == 1
    assert report.alerts[0].transaction_id == "t004"
    assert report.alerts[0].webhook_delivered
    assert report.summary == "Reviewed 12 transactions; flagged t004."

    body = json.loads(calls[0].content)
    assert body["transaction"]["customer_id"] == "c-100"
    ts = calls[0].headers["X-Risk-Timestamp"]
    assert calls[0].headers["X-Risk-Signature"] == sign("s3cret", ts, calls[0].content)

    # The profile tool returned real stats to the model.
    tool_result = model.requests[1][-1]["content"][0]["toolResult"]
    assert '"amount_max": 4850.0' in tool_result["content"][0]["text"]


def test_unknown_transaction_and_duplicates_are_not_sent():
    model = ScriptedModel(
        [
            [
                {"id": "1", "name": "notify_risk_team", "input": {"transaction_id": "nope", "risk_level": "high", "reasons": ["x"]}},
                {"id": "2", "name": "notify_risk_team", "input": {"transaction_id": "t008", "risk_level": "critical", "reasons": ["card testing"]}},
                {"id": "3", "name": "notify_risk_team", "input": {"transaction_id": "t008", "risk_level": "critical", "reasons": ["again"]}},
            ],
            [{"text": "done"}],
        ]
    )
    webhook, calls = recording_webhook()
    report = run_risk_monitor(SAMPLE, model=model, webhook=webhook)
    assert [a.transaction_id for a in report.alerts] == ["t008"]
    assert len(calls) == 1


def test_webhook_failure_is_reported_not_raised():
    model = ScriptedModel(
        [
            [{"id": "1", "name": "notify_risk_team", "input": {"transaction_id": "t004", "risk_level": "high", "reasons": ["x"]}}],
            [{"text": "done"}],
        ]
    )
    webhook, calls = recording_webhook(status=400)
    report = run_risk_monitor(SAMPLE, model=model, webhook=webhook)
    assert not report.alerts[0].webhook_delivered
    assert report.alerts[0].webhook_status == 400
    assert len(calls) == 1  # 4xx is not retried


def test_webhook_retries_on_5xx(monkeypatch):
    monkeypatch.setattr("risk_monitor.webhook.time.sleep", lambda s: None)
    webhook, calls = recording_webhook(status=503)
    result = webhook.send({"x": 1})
    assert not result.delivered and len(calls) == 3
    assert len({c.headers["Idempotency-Key"] for c in calls}) == 1


def test_webhook_rejects_plain_http():
    assert not WebhookClient(url="http://evil.example.com").send({}).delivered


@pytest.mark.parametrize(
    "payload",
    [
        {"transactions": []},
        {"transactions": [{**SAMPLE["transactions"][0], "amount": -5}]},
        {"transactions": [{**SAMPLE["transactions"][0], "timestamp": "2026-11-14T10:00:00"}]},
        {"transactions": [SAMPLE["transactions"][0], SAMPLE["transactions"][0]]},
    ],
)
def test_input_validation(payload):
    with pytest.raises(ValidationError):
        run_risk_monitor(payload, model=ScriptedModel([]))


def test_console_tracer_prints_each_step(capsys):
    from risk_monitor.trace import ConsoleTracer

    model = ScriptedModel(
        [
            [{"id": "1", "name": "get_customer_profile", "input": {"customer_id": "c-100"}}],
            [{"text": "All clear."}],
        ]
    )
    webhook, _ = recording_webhook()
    run_risk_monitor(SAMPLE, model=model, webhook=webhook, hooks=[ConsoleTracer()])
    err = capsys.readouterr().err
    for expected in [
        "Step 1: Agent started",
        "12 transactions from 4 customers",
        "Asking the LLM (call #1)",
        "wants to call get_customer_profile",
        "Running tool get_customer_profile",
        "✓ result",
        "Asking the LLM (call #2)",
        "💬 All clear.",
        "Agent finished",
    ]:
        assert expected in err, expected


def test_api_validation_errors_are_json_serializable():
    from risk_monitor.agentcore_app import invoke

    bad = {"transactions": [{**SAMPLE["transactions"][0], "amount": -5, "timestamp": "2026-11-14T10:00:00"}]}
    body = json.loads(json.dumps(invoke(bad)))  # raises if pydantic objects leak into the response
    assert body["error"] == "invalid_input"
    assert {tuple(d["loc"])[-1] for d in body["details"]} == {"amount", "timestamp"}
