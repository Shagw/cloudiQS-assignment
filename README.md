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

## Part 2: Deploy with Docker Compose

The agent ships as a container exposing the AgentCore Runtime HTTP contract:

| Endpoint | Purpose |
|---|---|
| `POST /invocations` | Body `{"transactions": [...]}` → returns the risk report |
| `GET /ping` | Health check |
| `GET /version` | `git_sha`, `prompt_sha`, `llm_provider`, `model_id` of what's running |

`docker-compose.yml` runs two services: `risk-monitor` (the API on `127.0.0.1:8080`) and `webhook-receiver` (a stand-in for the risk team, which verifies signatures and logs alerts).

```bash
# .env must contain GROQ_API_KEY=... and RISK_WEBHOOK_SECRET=...
docker compose up -d --build
curl -s localhost:8080/version
curl -s -X POST localhost:8080/invocations -H 'Content-Type: application/json' -d @examples/transactions.json
docker compose logs -f webhook-receiver      # watch alerts arrive
scripts/smoke_test.sh                         # health + version + validation + end-to-end check
docker compose down
```

Container hardening: a multi-stage build that ships no build tools, a non-root user, a read-only root filesystem, all Linux capabilities dropped, `no-new-privileges`, the port bound to localhost only, and secrets injected at runtime rather than baked into the image.

### Configuration
| What | Where | Versioned? |
|---|---|---|
| System prompt | `src/risk_monitor/prompts/system_prompt.md` | yes (baked into the image) |
| Runtime config (provider, model IDs, region) | `config/agent.env` | yes (read at deploy time) |
| Secrets (`GROQ_API_KEY`, `RISK_WEBHOOK_SECRET`) | `.env` locally / GitHub Actions secrets in CI | **no** |

## Part 3: CI/CD pipeline (GitHub Actions)

`.github/workflows/ci-cd.yml`:

```
push / PR ─► test ─► build ─────────────────► deploy (main only, self-hosted runner on the Mac)
             pytest   multi-arch image          docker compose pull + up --wait
                      pushed to GHCR as          smoke test (expects git_sha == commit)
                      ghcr.io/<repo>:<sha>       roll back to previous image if it fails
```

- **PRs** run the tests and build the image without pushing it. They never reach the deploy machine.
- **Build once, deploy that artifact.** The image deployed is exactly the one built and tagged with the commit SHA.
- **Deploys are serialised** (`concurrency: deploy-production`) and gated by the `production` environment.

### How a prompt or config change reaches the running deployment
1. Edit `src/risk_monitor/prompts/system_prompt.md` (or a value in `config/agent.env`), then commit and push to `main`.
2. **test**: the unit tests run against the change.
3. **build**: a new image `ghcr.io/<repo>:<new-sha>` is built with the new prompt inside it.
4. **deploy**: the runner checks out the new commit (picking up `config/agent.env`), pulls the new image, and recreates the container.
5. **smoke test**: `GET /version` must report the new `git_sha`, and the sample batch must be processed end to end with all webhooks delivered. Otherwise the previous image is restored.
6. Confirm the change yourself: `curl localhost:8080/version` shows a new `git_sha`, and a new `prompt_sha` if the prompt changed.

### One-time setup
1. **Secrets** (repo → Settings → Secrets and variables → Actions): `GROQ_API_KEY`, `RISK_WEBHOOK_SECRET`.
2. **Environment** (Settings → Environments): create `production`. Optionally add required reviewers so a human approves each deploy.
3. **Self-hosted runner** (Settings → Actions → Runners → New self-hosted runner → macOS ARM64): follow GitHub's download and `config.sh` steps, adding `--labels risk-monitor-deploy`, then `./run.sh`. Docker Desktop must be running.
4. **Fork PR safety**: Settings → Actions → General → "Require approval for all external contributors". A self-hosted runner executes workflow code on your machine.

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
