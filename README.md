# Risk Monitor Agent

A [Strands](https://strandsagents.com) agent that reviews a batch of transactions, uses an LLM to decide which ones look suspicious, sends each suspicious one to the risk team's webhook, and returns a summary. It runs on Amazon Bedrock (Claude) or Groq, and is packaged for Bedrock AgentCore Runtime.

## How it works

```
payload ──► pydantic validation ──► Strands Agent (LLM: Bedrock or Groq)
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

### Prerequisites
- [uv](https://docs.astral.sh/uv/) (installs Python 3.12 and the dependencies for you)
- [Docker Desktop](https://www.docker.com/products/docker-desktop/), for Part 2
- An LLM: a [Groq](https://console.groq.com) API key (what the deployment uses now), **or** an AWS account with Bedrock model access

### Steps
```bash
git clone https://github.com/Shagw/cloudiQS-assignment.git
cd cloudiQS-assignment
uv sync --extra groq          # installs dependencies (the groq extra adds the OpenAI-compatible client)
cp .env.example .env          # then edit .env and put in your real GROQ_API_KEY and a webhook secret
uv run pytest                 # optional: 11 offline tests, no API key needed
```

### Choosing the LLM
The provider is set in `config/agent.env`:

| Provider | Settings |
|---|---|
| Groq (current) | `LLM_PROVIDER=groq`, `GROQ_MODEL_ID=openai/gpt-oss-120b`, plus `GROQ_API_KEY` in `.env` |
| Amazon Bedrock | `LLM_PROVIDER=bedrock`, `BEDROCK_MODEL_ID=us.anthropic.claude-opus-5-5`, `AWS_REGION=us-east-1`, plus AWS credentials (`aws configure`) and model access enabled in the Bedrock console |

The agent is built for Bedrock, but my new AWS account is still waiting for Bedrock model access from AWS Support, so for now it is tested and deployed with Groq. Switching to Bedrock is a one-line change in `config/agent.env` pushed through the pipeline.

## Run locally

```bash
# load the config and secrets into your shell (do this in every new terminal)
set -a; source config/agent.env; source .env; set +a

# terminal 1: a webhook receiver that checks the signature and prints alerts
uv run python scripts/webhook_receiver.py

# terminal 2: run the agent from the CLI
uv run --extra groq risk-monitor examples/transactions.json

# see every step (LLM calls, tool calls, webhook results) in the console
uv run --extra groq risk-monitor examples/transactions.json --trace
# same, but pause before each step until you press Enter
uv run --extra groq risk-monitor examples/transactions.json --step

# or as the AgentCore HTTP service
uv run --extra groq python -m risk_monitor.agentcore_app
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

### Sending your own transactions (JSON or CSV)
```bash
scripts/check.sh examples/transactions.csv               # or any .json / .csv file
scripts/check.sh my_data.json -o report.json             # also save the full report
RISK_API_URL=https://my-host scripts/check.sh data.csv   # target another deployment
```
CSV needs a header row: `transaction_id,customer_id,amount,merchant,timestamp[,currency]`. Files with more than 500 transactions are split into batches automatically, and each customer's transactions stay in the same batch so the agent sees their full history.

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
3. **Self-hosted runner** (Settings → Actions → Runners → New self-hosted runner → macOS ARM64): follow GitHub's download and `config.sh` steps, adding `--labels risk-monitor-deploy`, then `./svc.sh install && ./svc.sh start` to run it in the background as a service (or `./run.sh` to run it in a terminal). Docker Desktop must be running.
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

## Part 4: Written answers

### 1. Updating the prompt without breaking existing users

The prompt is kept in its own file (`src/risk_monitor/prompts/system_prompt.md`), so any change goes through git like normal code. Every build creates a Docker image tagged with the commit SHA, and `/version` returns the `git_sha` and a hash of the prompt (`prompt_sha`), so I can always check which version is live. The response format is fixed by a Pydantic model, so changing the prompt can change what gets flagged, but not the shape of the JSON that users receive.

For versioning I would treat the API and the prompt separately. The API (request/response format) gets versions like `/v1`, `/v2`. Only additive changes go into the same version. The prompt and model are more of a behaviour version, which users can see through `/version`.

Before shipping a prompt change I would:
- run the new prompt against a set of transactions where we already know the right answer, and compare the results with the current prompt. My unit tests use a fake LLM, so they test the code but not the quality of the AI's decisions.
- run the new version next to the old one on real traffic for a while without sending webhooks, and compare what each one flags
- then move traffic over slowly, or let one customer try it first
- turn on required reviewers for the `production` environment in GitHub, so a deploy needs someone's approval (not enabled in this repo yet)

### 2. Rolling back when the new version gives false positives

Old images stay in GHCR under their commit SHA, so the quickest rollback is to start the previous one again. No rebuild needed:

```bash
IMAGE=ghcr.io/shagw/cloudiqs-assignment:<last-good-sha> docker compose up -d --no-build --wait
curl localhost:8080/version   # check the old git_sha / prompt_sha is back
```

After that I would `git revert` the bad commit and push, so main matches what is running and the pipeline redeploys it properly.

The pipeline already rolls back automatically if the smoke test fails after a deploy. But false positives won't fail a smoke test, because the service is still working, just making bad calls. To catch that, I would track the flag rate (flagged / reviewed) per version and send an alert if it suddenly goes up.

### 3. Keeping two customers separate

With only two customers, I would give each one their own deployment: separate container, separate env file, their own webhook URL and secret, and separate logs. Nothing is shared, so one customer's data can't end up in the other's results. With this setup it's just config, e.g. `docker compose -p customer-a --env-file customer-a.env up -d`.

If both had to share one deployment:
- each customer gets their own API key, and the server works out who the customer is from the key, not from anything in the request body
- the webhook URL and secret are looked up per customer from a secrets store (like AWS Secrets Manager), never taken from the request, so nobody can redirect alerts
- the agent already starts fresh for every request. The tools only see the transactions in that request, and nothing is remembered between requests, so there's no shared memory between customers. On AgentCore Runtime each session also runs in its own isolated microVM.
- results are currently just returned in the response, not stored. If we stored them, every record would have a `tenant_id` and access would be restricted per customer at the database level.
- rate limits per customer, so one can't use up all the LLM capacity
