"""The course LLM gateway as a Strands model: Strands' OpenAI provider with the gateway's base URL
(Strands docs, "OpenAI": client_args with api_key and base_url reach any OpenAI-compatible server)."""
from __future__ import annotations

from strands.models.openai import OpenAIModel

from grocery import config
from grocery.model import sampling


def gateway_model(model_id: str, max_tokens: int = 3000, base_url: str | None = None, token: str | None = None):
    base_url, token = base_url or config.GATEWAY_URL, token or config.token()
    if not base_url or not token:
        raise SystemExit("set GATEWAY_URL and COURSE_MCP_TOKEN first (README, 'Run it')")
    return OpenAIModel(client_args={"api_key": token, "base_url": base_url}, model_id=model_id,
                       params={"max_tokens": max_tokens, **sampling(model_id)})
