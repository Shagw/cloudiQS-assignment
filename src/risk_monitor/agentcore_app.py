"""HTTP service for the agent (Bedrock AgentCore Runtime contract: POST /invocations, GET /ping).

Local:  uv run python -m risk_monitor.agentcore_app   (serves on :8080)
"""

import logging
import os

from bedrock_agentcore.runtime import BedrockAgentCoreApp
from pydantic import ValidationError
from starlette.requests import Request
from starlette.responses import JSONResponse

from .agent import PROMPT_SHA, run_risk_monitor

logging.basicConfig(level=logging.INFO)
app = BedrockAgentCoreApp()


def version_info() -> dict:
    """What's running: lets you confirm a pushed code/prompt/config change reached the deployment."""
    provider = os.environ.get("LLM_PROVIDER", "bedrock")
    model = os.environ.get("GROQ_MODEL_ID" if provider == "groq" else "BEDROCK_MODEL_ID", "default")
    return {
        "git_sha": os.environ.get("GIT_SHA", "dev"),
        "prompt_sha": PROMPT_SHA,
        "llm_provider": provider,
        "model_id": model,
    }


async def version(request: Request) -> JSONResponse:
    return JSONResponse(version_info())


app.add_route("/version", version, methods=["GET"])


@app.entrypoint
def invoke(payload: dict) -> dict:
    try:
        return run_risk_monitor(payload).model_dump(mode="json")
    except ValidationError as e:
        return {"error": "invalid_input", "details": e.errors(include_url=False, include_input=False)}


if __name__ == "__main__":
    logging.getLogger(__name__).info("starting %s", version_info())
    app.run(host=os.environ.get("HOST"))
