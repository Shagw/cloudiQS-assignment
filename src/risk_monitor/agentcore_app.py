"""Bedrock AgentCore Runtime entrypoint.

Local:  uv run python -m risk_monitor.agentcore_app   (serves POST /invocations on :8080)
"""

import logging

from bedrock_agentcore.runtime import BedrockAgentCoreApp
from pydantic import ValidationError

from .agent import run_risk_monitor

logging.basicConfig(level=logging.INFO)
app = BedrockAgentCoreApp()


@app.entrypoint
def invoke(payload: dict) -> dict:
    try:
        return run_risk_monitor(payload).model_dump(mode="json")
    except ValidationError as e:
        return {"error": "invalid_input", "details": e.errors(include_url=False, include_input=False)}


if __name__ == "__main__":
    app.run()
