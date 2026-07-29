"""The MCP protocol layer: JSON-RPC framing, handshake, tool dispatch.

Knows nothing about Vikunja. It is handed a fixed list of :class:`Tool` objects
and can call those and nothing else — there is no generic passthrough, no
"execute" tool and no way for a request to name an operation that is not in the
list. That is what makes "this connection cannot edit, close, delete or comment"
a property of the code rather than a promise about the prompt.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Callable, Sequence

SERVER_NAME = "vikunja-claude-mcp"
SERVER_VERSION = "1.0.0"

# Versions this server implements. A client asking for one of these gets it
# back; a client asking for anything else is told what we do speak rather than
# being silently agreed with.
SUPPORTED_PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")
LATEST_PROTOCOL_VERSION = SUPPORTED_PROTOCOL_VERSIONS[0]

# JSON-RPC 2.0 error codes.
PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603


class JsonRpcError(Exception):
    """A protocol-level failure: the request itself was not usable."""

    def __init__(self, code: int, message: str, data: Any = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.data = data

    def payload(self) -> dict[str, Any]:
        error: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.data is not None:
            error["data"] = self.data
        return error


class ToolError(Exception):
    """A tool refused, or could not do, what was asked.

    Distinct from :class:`JsonRpcError` on purpose. This is not a malformed
    request — it is a well-formed one that the boundary declined, and the model
    is meant to read the reason and act on it, so it comes back as a tool result
    flagged ``isError`` rather than as a transport failure.
    """


@dataclass(frozen=True)
class Tool:
    """One callable operation, its schema and its implementation together.

    Kept as one object so a tool cannot be advertised without an implementation
    or implemented without being advertised.
    """

    name: str
    title: str
    description: str
    input_schema: dict[str, Any]
    run: Callable[[dict[str, Any]], Any]
    annotations: dict[str, Any] = field(default_factory=dict)

    def descriptor(self) -> dict[str, Any]:
        """The `tools/list` shape. Never includes `run`."""
        return {
            "name": self.name,
            "title": self.title,
            "description": self.description,
            "inputSchema": self.input_schema,
            "annotations": self.annotations,
        }

    def required_arguments(self) -> list[str]:
        return list(self.input_schema.get("required") or [])


def _tool_result(payload: Any) -> dict[str, Any]:
    result: dict[str, Any] = {
        "content": [{"type": "text", "text": json.dumps(payload, indent=2, default=str)}]
    }
    if isinstance(payload, dict):
        result["structuredContent"] = payload
    return result


def _tool_failure(message: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": message}], "isError": True}


class McpProtocol:
    def __init__(self, tools: Sequence[Tool]):
        self.tools = list(tools)
        self._by_name = {tool.name: tool for tool in self.tools}
        if len(self._by_name) != len(self.tools):
            raise ValueError("two tools share a name")

    @property
    def tool_names(self) -> set[str]:
        return set(self._by_name)

    # -- entry point -------------------------------------------------------

    def handle(self, message: Any) -> dict[str, Any] | None:
        """Answer one JSON-RPC message, or return None if it wants no answer."""
        if isinstance(message, list):
            # Batching was removed from MCP in 2025-06-18. Refusing is honest;
            # answering only the first element of a batch would not be.
            raise JsonRpcError(
                INVALID_REQUEST, "JSON-RPC batches are not supported"
            )
        if not isinstance(message, dict):
            raise JsonRpcError(INVALID_REQUEST, "Request must be a JSON object")

        method = message.get("method")
        if not isinstance(method, str):
            raise JsonRpcError(INVALID_REQUEST, "Request has no method")

        request_id = message.get("id")
        params = message.get("params") or {}
        if not isinstance(params, dict):
            raise JsonRpcError(INVALID_PARAMS, "params must be an object")

        # No id means a notification: the sender is not waiting for a reply, and
        # the spec forbids sending one even to report that the method is unknown.
        if request_id is None:
            return None

        try:
            result = self._invoke(method, params)
        except JsonRpcError as exc:
            return {"jsonrpc": "2.0", "id": request_id, "error": exc.payload()}
        return {"jsonrpc": "2.0", "id": request_id, "result": result}

    # -- methods -----------------------------------------------------------

    def _invoke(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        if method == "initialize":
            return self._initialize(params)
        if method == "ping":
            return {}
        if method == "tools/list":
            return {"tools": [tool.descriptor() for tool in self.tools]}
        if method == "tools/call":
            return self._call_tool(params)
        raise JsonRpcError(METHOD_NOT_FOUND, f"Unknown method {method!r}")

    def _initialize(self, params: dict[str, Any]) -> dict[str, Any]:
        requested = params.get("protocolVersion")
        version = (
            requested
            if requested in SUPPORTED_PROTOCOL_VERSIONS
            else LATEST_PROTOCOL_VERSION
        )
        return {
            "protocolVersion": version,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
        }

    def _call_tool(self, params: dict[str, Any]) -> dict[str, Any]:
        name = params.get("name")
        tool = self._by_name.get(name) if isinstance(name, str) else None
        if tool is None:
            raise JsonRpcError(
                INVALID_PARAMS,
                f"Unknown tool {name!r}. This connection exposes only: "
                + ", ".join(sorted(self._by_name)),
            )

        arguments = params.get("arguments") or {}
        if not isinstance(arguments, dict):
            raise JsonRpcError(INVALID_PARAMS, "arguments must be an object")

        missing = [
            key
            for key in tool.required_arguments()
            if arguments.get(key) is None
        ]
        if missing:
            raise JsonRpcError(
                INVALID_PARAMS,
                f"{tool.name} requires {', '.join(missing)}",
            )

        try:
            return _tool_result(tool.run(arguments))
        except ToolError as exc:
            return _tool_failure(str(exc))
        except (TypeError, ValueError) as exc:
            # An argument of the declared name but the wrong shape — "abc" for
            # an integer id. That is a bad request, not a failed operation.
            raise JsonRpcError(
                INVALID_PARAMS, f"{tool.name}: unusable argument ({exc})"
            ) from exc
