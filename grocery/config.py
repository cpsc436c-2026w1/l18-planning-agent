"""Settings, all from the environment. Nothing secret is written in code or in the repo.

GATEWAY_URL        base URL of the course LLM gateway (default: the course's), ending in /v1
KB_MCP_TOKEN       your course token; the course LLM gateway takes it as the API key and charges your budget
                   (LLM_API_KEY, then COURSE_MCP_TOKEN, are read if KB_MCP_TOKEN is not set)
A .env file in the repository root (git-ignored) may hold these as KEY=VALUE lines.
PLANNER_MODEL      the model that plans and merges (default qwen3.8-27b, the stronger course model)
WORKER_MODEL       the model that works on one dish (default qwen3.6-35b-a3b, the cheaper course model)
GROCERY_STORE      duckdb (default: the laptop) or dynamodb (AWS)
GROCERY_PREFIX     first part of the two DynamoDB table names (default l18-grocery)
AWS_REGION         default ca-central-1
"""
from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_FILE = Path(os.environ.get("GROCERY_DATA") or ROOT / "data" / "products.jsonl")
OUTPUT_DIR = ROOT / "output"

# Routing: the planner call decides the whole week, so it goes to the stronger model; a worker call picks one
# product from five and the merge call writes up checked numbers, so they go to the cheaper one
# (AI Engineering ch. 10, "Router": route simpler queries to cheaper models). Change either with an
# environment variable. Until the course gateway offers stronger models, both are its Qwen models.
PLANNER_MODEL = os.environ.get("PLANNER_MODEL", "qwen3.8-27b")
WORKER_MODEL = os.environ.get("WORKER_MODEL", "qwen3.6-35b-a3b")

def _load_dotenv() -> None:
    """KEY=VALUE lines from ROOT/.env, for variables not already set. The file is git-ignored."""
    f = ROOT / ".env"
    if f.is_file() and not os.environ.get("L18_NO_DOTENV"):
        for line in f.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip().removeprefix("export ").strip(), v.strip().strip('"').strip("'"))


_load_dotenv()
DEFAULT_GATEWAY_URL = "https://co4zo6ssflbun6deagtswqresm0vrmyt.lambda-url.ca-central-1.on.aws/v1"
GATEWAY_URL = (os.environ.get("GATEWAY_URL", "").strip() or DEFAULT_GATEWAY_URL).rstrip("/")
REGION = os.environ.get("AWS_REGION", "ca-central-1")
PREFIX = os.environ.get("GROCERY_PREFIX", "l18-grocery")
PRODUCTS_TABLE = f"{PREFIX}-products"
LISTS_TABLE = f"{PREFIX}-lists"


def token() -> str:
    """The course token, read when it is needed and never stored in a result, a trace or a printed line."""
    for name in ("KB_MCP_TOKEN", "LLM_API_KEY", "COURSE_MCP_TOKEN"):
        if os.environ.get(name, "").strip():
            return os.environ[name].strip()
    return ""
