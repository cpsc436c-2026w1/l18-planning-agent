"""behave set-up. Run from the repo folder: `uv run behave`. Every scenario is offline: no key, no account."""
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def before_scenario(context, scenario):
    for name in ("GATEWAY_URL", "COURSE_MCP_TOKEN", "AWS_PROFILE"):
        os.environ.pop(name, None)
    context.planner = context.worker = context.agent_model = None
    context.asked = []
    context.write_keys = []
