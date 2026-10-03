# SPDX-License-Identifier: MIT
"""MCP v1 Streamable HTTP binding, compatible with an operator-hosted protein server."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from urllib.parse import urlsplit

from proteinrsi.contracts import digest
from proteinrsi.tools import ToolGateway, ToolSpec


class MCPExecutor:
    def __init__(self, url: str, remote_tool: str, expected_schema_sha256: str):
        parsed = urlsplit(url)
        if parsed.scheme != "https" and not (parsed.scheme == "http" and parsed.hostname in ("127.0.0.1", "localhost")):
            raise ValueError("MCP transport requires HTTPS or localhost")
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("No credentials/query strings in MCP endpoint URLs")
        self.url, self.remote_tool = url, remote_tool
        self.expected_schema_sha256 = expected_schema_sha256

    async def _call(self, arguments: dict) -> dict:
        from mcp import ClientSession
        from mcp.client.streamable_http import streamablehttp_client
        async with streamablehttp_client(self.url) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                tools = (await session.list_tools()).tools
                tool = next((t for t in tools if t.name == self.remote_tool), None)
                if tool is None or digest(tool.inputSchema) != self.expected_schema_sha256:
                    raise ValueError("Remote tool missing or input schema changed; re-audit binding")
                result = await session.call_tool(self.remote_tool, arguments)
                if result.isError:
                    raise RuntimeError("MCP server reported a tool error")
                structured = getattr(result, "structuredContent", None)
                if isinstance(structured, dict):
                    return structured
                text = "\n".join(c.text for c in result.content if getattr(c, "type", None) == "text")
                data = json.loads(text)
                if not isinstance(data, dict):
                    raise ValueError("Expected structured object from remote tool")
                return data

    def __call__(self, arguments: dict) -> dict:
        return asyncio.run(self._call(arguments))


def load_bindings(gateway: ToolGateway, path: str | Path) -> None:
    """Bindings are operator configuration, never generated/approved by an agent."""
    bindings = json.loads(Path(path).read_text())
    for binding in bindings:
        spec = ToolSpec.model_validate(binding["spec"])
        parsed = urlsplit(binding["url"])
        if parsed.hostname not in ("localhost", "127.0.0.1") and not spec.data_egress:
            raise ValueError("Remote endpoint must explicitly declare data_egress=true")
        gateway.register(spec, MCPExecutor(binding["url"], binding["remote_tool"],
                                          binding["remote_input_schema_sha256"]))
