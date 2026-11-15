"""Risk Monitor agent: reviews a batch of transactions, alerts the risk team, returns a report."""

import hashlib
import json
import os
import threading
from importlib.resources import files

from botocore.config import Config
from strands import Agent
from strands.hooks import HookProvider
from strands.models import BedrockModel, Model

from .models import Alert, RiskReport, RiskRequest
from .tools import get_customer_profile, notify_risk_team
from .webhook import WebhookClient

DEFAULT_MODEL_ID = "us.anthropic.claude-opus-5-5"
DEFAULT_GROQ_MODEL_ID = "openai/gpt-oss-120b"

SYSTEM_PROMPT = (files("risk_monitor") / "prompts" / "system_prompt.md").read_text()
PROMPT_SHA = hashlib.sha256(SYSTEM_PROMPT.encode()).hexdigest()[:12]


def build_model() -> Model:
    # LLM_PROVIDER=groq is a local-testing fallback; Bedrock is the production path.
    if os.environ.get("LLM_PROVIDER", "bedrock") == "groq":
        from strands.models.openai import OpenAIModel

        return OpenAIModel(
            client_args={"api_key": os.environ["GROQ_API_KEY"], "base_url": "https://api.groq.com/openai/v1"},
            model_id=os.environ.get("GROQ_MODEL_ID", DEFAULT_GROQ_MODEL_ID),
            params={"max_tokens": 8000, "temperature": 0, "extra_body": {"include_reasoning": False}},
        )

    return BedrockModel(
        model_id=os.environ.get("BEDROCK_MODEL_ID", DEFAULT_MODEL_ID),
        region_name=os.environ.get("AWS_REGION", "us-east-1"),
        max_tokens=16000,
        boto_client_config=Config(retries={"max_attempts": 4, "mode": "adaptive"}, read_timeout=300),
    )


def run_risk_monitor(
    payload: dict,
    model: Model | None = None,
    webhook: WebhookClient | None = None,
    hooks: list[HookProvider] | None = None,
) -> RiskReport:
    """Validate the input, run the agent, and assemble a report from what the tools recorded."""
    request = RiskRequest.model_validate(payload)
    alerts: dict[str, Alert] = {}

    agent = Agent(
        model=model or build_model(),
        system_prompt=SYSTEM_PROMPT,
        tools=[get_customer_profile, notify_risk_team],
        callback_handler=None,
        hooks=hooks,
    )
    batch = [t.model_dump(mode="json") for t in request.transactions]
    result = agent(
        f"Review these {len(batch)} transactions.\n<transactions>\n{json.dumps(batch, indent=1)}\n</transactions>",
        invocation_state={
            "transactions": {t.transaction_id: t for t in request.transactions},
            "alerts": alerts,
            "claimed": set(),
            "lock": threading.Lock(),
            "webhook": webhook or WebhookClient(),
        },
    )

    # Alerts come from actual tool executions, not the model's prose, so the report can't
    # claim a notification that was never sent.
    return RiskReport(
        transactions_reviewed=len(request.transactions),
        flagged_count=len(alerts),
        alerts=list(alerts.values()),
        summary=str(result).strip(),
    )
