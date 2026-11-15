# Risk Monitor Agent

A [Strands](https://strandsagents.com) agent on Amazon Bedrock that reviews a batch of transactions, uses an LLM to decide which ones look suspicious, sends each suspicious one to the risk team's webhook, and returns a summary. It is packaged for Bedrock AgentCore Runtime.

## How it works

```
payload ──► pydantic validation ──► Strands Agent (Claude on Bedrock)
                                        │
                     ┌──────────────────┴──────────────────┐
         get_customer_profile(customer_id)      notify_risk_team(txn_id, level, reasons)
         stats for that customer's baseline     ──► signed HTTPS POST to RISK_WEBHOOK_URL
                                        │
                     RiskReport { transactions_reviewed, flagged_count, alerts[], summary }
```

- **The LLM does the reasoning.** A deterministic tool supplies per-customer baselines (mean/median/max spend, merchants, shortest gap between transactions). The model weighs signals such as spend spikes, velocity/card testing, high-risk merchants and odd hours.
- **The report reflects real deliveries.** `alerts` is built from tool executions that actually ran, including the webhook HTTP status. It never comes from the model's prose.
- **Per-request isolation.** The batch, the alert log and the webhook client travel in Strands `invocation_state`, so concurrent AgentCore sessions share nothing.

### Security measures
| Concern | Mitigation |
|---|---|
| Prompt injection via merchant names and other fields | Data is wrapped in `<transactions>` tags, the system prompt marks it as untrusted, and injected text is itself treated as a risk signal (see `t012` in the sample) |
| Model inventing transactions or spamming alerts | `notify_risk_team` rejects IDs outside the batch, de-duplicates, and caps the number and length of reasons |
| Webhook spoofing or replay | HMAC-SHA256 signature over `timestamp.body` (`X-Risk-Signature`, `X-Risk-Timestamp`) |
| Duplicate alerts on retry | `Idempotency-Key` header is the same across retries; retries only on 408/429/5xx with backoff |
| Exfiltration over plaintext | Only `https://` webhooks are allowed (localhost is exempt for development) |
| Bad input | Strict pydantic schema: positive amounts, timezone-aware timestamps, unique IDs, at most 500 transactions per batch |

## Setup

```bash
uv sync
export AWS_REGION=us-east-1                       # a region where your Claude model is enabled
export BEDROCK_MODEL_ID=us.anthropic.claude-opus-5-5 # US cross-region inference profile
export RISK_WEBHOOK_URL=http://localhost:9000
export RISK_WEBHOOK_SECRET=dev-secret
```

## Run locally

```bash
# terminal 1: a webhook receiver that checks the signature and prints alerts
RISK_WEBHOOK_SECRET=dev-secret uv run python scripts/webhook_receiver.py

# terminal 2: run the agent from the CLI
uv run risk-monitor examples/transactions.json

# see every step (LLM calls, tool calls, webhook results) in the console
uv run risk-monitor examples/transactions.json --trace
# same, but pause before each step until you press Enter
uv run risk-monitor examples/transactions.json --step

# or as the AgentCore HTTP service
uv run python -m risk_monitor.agentcore_app
curl -X POST localhost:8080/invocations -H 'Content-Type: application/json' -d @examples/transactions.json
```

## Tests

```bash
uv run pytest
```
The tests run offline. A scripted Strands `Model` drives the real agent loop and tools, and an `httpx.MockTransport` stands in for the webhook.

## Input / output

Input: `{"transactions": [{"transaction_id", "customer_id", "amount", "merchant", "timestamp", "currency"?}]}`

Output:
```json
{
  "transactions_reviewed": 12,
  "flagged_count": 3,
  "alerts": [{"transaction_id": "t008", "customer_id": "c-200", "risk_level": "critical",
              "reasons": ["..."], "webhook_delivered": true, "webhook_status": 204}],
  "summary": "..."
}
```
