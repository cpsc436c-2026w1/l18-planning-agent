"""The grocery tools through an AgentCore Gateway: an MCP endpoint with AWS_IAM inbound auth.

The Gateway turns the tools Lambda into MCP tools named `<target>___<tool>` (grocery-tools___search_products).
A caller signs each request with its AWS credentials (SigV4, service name bedrock-agentcore): the Runtime's
execution role inside AgentCore, your own profile on the laptop. `GatewaySigV4` is the pilot's 15-line signer.
"""
from __future__ import annotations

import hashlib
import json
import uuid

import boto3
import httpx
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest
from strands.tools.mcp import MCPClient

GROCERY_TOOLS = ("search_products", "get_product", "get_list", "add_to_list")


class GatewaySigV4(httpx.Auth):
    """Sign every request to the Gateway with the caller's AWS credentials."""
    requires_request_body = True

    def __init__(self, session: boto3.Session, region: str):
        self.creds, self.region = session.get_credentials(), region

    def auth_flow(self, request: httpx.Request):
        body = request.content or b""
        aws_req = AWSRequest(method=request.method, url=str(request.url), data=body,
                             headers={"content-type": request.headers.get("content-type", "application/json")})
        SigV4Auth(self.creds.get_frozen_credentials(), "bedrock-agentcore", self.region).add_auth(aws_req)
        for k, v in aws_req.headers.items():
            request.headers[k] = v
        request.headers["x-amz-content-sha256"] = hashlib.sha256(body).hexdigest()
        yield request


def open_gateway(url: str, region: str, profile: str | None = None) -> MCPClient:
    session = boto3.Session(profile_name=profile, region_name=region)
    client = MCPClient(url=url, auth_provider=GatewaySigV4(session, region))
    client.start()
    return client


class GatewayGrocery:
    """What app code (the hooks, the worker tool) needs from the catalogue, read through the Gateway's tools."""

    def __init__(self, client: MCPClient, categories: list[str]):
        self.client, self._categories = client, categories
        listed = client.list_tools_sync()
        # short name -> the MCP tool, for the four grocery tools only (the Gateway also lists the lake tools)
        self.tools = {t.tool_name.split("___")[-1]: t for t in listed
                      if t.tool_name.split("___")[-1] in GROCERY_TOOLS and "grocery" in t.tool_name}

    def _call(self, name: str, arguments: dict) -> dict:
        result = self.client.call_tool_sync(uuid.uuid4().hex, self.tools[name].tool_name, arguments)
        text = " ".join(c.get("text", "") for c in result.get("content", []))
        try:
            found = json.loads(text)
        except json.JSONDecodeError:
            return {"error": text[:200] or "empty tool result"}
        return found if isinstance(found, dict) else {"error": text[:200]}

    def get_product(self, code: str) -> dict:
        return self._call("get_product", {"code": code})

    def get_list(self, shopper: str) -> dict:
        return self._call("get_list", {"shopper": shopper})

    def categories(self) -> list[str]:
        return self._categories
