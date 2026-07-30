"""The MCP envelope: handshake, tool advertisement, and what is refused.

The tool set is asserted as an exact set rather than a subset. A subset check
would still pass on the day something adds `close_task`, which is the one thing
this boundary exists to prevent.
"""

from __future__ import annotations

import unittest
from typing import Any

from vikunja_claude.mcp import (
    INVALID_PARAMS,
    INVALID_REQUEST,
    LATEST_PROTOCOL_VERSION,
    METHOD_NOT_FOUND,
    SUPPORTED_PROTOCOL_VERSIONS,
    JsonRpcError,
    McpProtocol,
    Tool,
    ToolError,
)

from .support import McpTestCase

EXPOSED_TOOLS = {"get_task", "list_open_tasks", "create_task"}


def request(
    method: str, params: dict | None = None, request_id: Any = 1
) -> dict[str, Any]:
    message: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
    if request_id is not None:
        message["id"] = request_id
    if params is not None:
        message["params"] = params
    return message


class TestHandshake(McpTestCase):
    def test_initialize_agrees_to_a_version_it_speaks(self):
        for version in SUPPORTED_PROTOCOL_VERSIONS:
            with self.subTest(version=version):
                response = self.protocol.handle(
                    request("initialize", {"protocolVersion": version})
                )
                self.assertEqual(response["result"]["protocolVersion"], version)

    def test_initialize_names_its_own_version_for_one_it_does_not_speak(self):
        """Never echo an unknown version back: that claims support we lack."""
        response = self.protocol.handle(
            request("initialize", {"protocolVersion": "1999-01-01"})
        )
        self.assertEqual(
            response["result"]["protocolVersion"], LATEST_PROTOCOL_VERSION
        )

    def test_initialize_declares_tools_and_identifies_the_server(self):
        result = self.protocol.handle(request("initialize", {}))["result"]
        self.assertIn("tools", result["capabilities"])
        self.assertEqual(result["serverInfo"]["name"], "vikunja-claude-mcp")

    def test_ping_is_answered(self):
        self.assertEqual(self.protocol.handle(request("ping"))["result"], {})


class TestAdvertisedSurface(McpTestCase):
    def test_exactly_the_expected_tools_are_exposed(self):
        tools = self.protocol.handle(request("tools/list"))["result"]["tools"]
        self.assertEqual({tool["name"] for tool in tools}, EXPOSED_TOOLS)

    def test_no_tool_advertises_a_way_to_change_an_existing_task(self):
        forbidden = ("update", "edit", "close", "delete", "comment", "move", "assign")
        for name in self.protocol.tool_names:
            for verb in forbidden:
                self.assertNotIn(verb, name)

    def test_every_tool_declares_a_closed_schema(self):
        """additionalProperties: False — a client cannot smuggle an extra field."""
        tools = self.protocol.handle(request("tools/list"))["result"]["tools"]
        for tool in tools:
            with self.subTest(tool=tool["name"]):
                schema = tool["inputSchema"]
                self.assertEqual(schema["type"], "object")
                self.assertFalse(schema["additionalProperties"])
                # `required` is declared, and names only arguments that exist.
                # Not that it is non-empty: a listing that takes nothing but
                # optional filters has nothing to require, and a name here that
                # is not a property is a tool no client could ever call.
                self.assertIsInstance(schema["required"], list)
                self.assertEqual(
                    [k for k in schema["required"] if k not in schema["properties"]], []
                )

    def test_the_listing_requires_no_arguments(self):
        """Its filters are optional, so "list the open tickets" is a valid call."""
        tools = {
            tool["name"]: tool
            for tool in self.protocol.handle(request("tools/list"))["result"]["tools"]
        }
        self.assertEqual(tools["list_open_tasks"]["inputSchema"]["required"], [])
        self.assertEqual(
            set(tools["list_open_tasks"]["inputSchema"]["properties"]),
            {"bucket", "label"},
        )

    def test_the_read_tools_are_marked_read_only_and_the_write_tool_is_not(self):
        tools = {
            tool["name"]: tool
            for tool in self.protocol.handle(request("tools/list"))["result"]["tools"]
        }
        self.assertTrue(tools["get_task"]["annotations"]["readOnlyHint"])
        self.assertTrue(tools["list_open_tasks"]["annotations"]["readOnlyHint"])
        self.assertFalse(tools["create_task"]["annotations"]["readOnlyHint"])

    def test_the_create_tool_tells_the_model_to_confirm_first(self):
        """The client-side confirmation is the only enforcement of 'explicit
        user instruction', so the tool has to actually ask for it."""
        tools = {
            tool["name"]: tool
            for tool in self.protocol.handle(request("tools/list"))["result"]["tools"]
        }
        description = tools["create_task"]["description"].lower()
        self.assertIn("explicitly asked", description)
        self.assertIn("showing them the exact title and description", description)

    def test_listing_never_leaks_the_implementation(self):
        tools = self.protocol.handle(request("tools/list"))["result"]["tools"]
        for tool in tools:
            self.assertNotIn("run", tool)


class TestRefusals(McpTestCase):
    def test_an_unknown_method_is_not_found(self):
        response = self.protocol.handle(request("tasks/delete"))
        self.assertEqual(response["error"]["code"], METHOD_NOT_FOUND)

    def test_an_unknown_tool_is_refused_and_told_what_exists(self):
        response = self.protocol.handle(
            request("tools/call", {"name": "close_task", "arguments": {"task_id": 9}})
        )
        self.assertEqual(response["error"]["code"], INVALID_PARAMS)
        self.assertIn("get_task", response["error"]["message"])
        self.assertIn("create_task", response["error"]["message"])

    def test_a_missing_required_argument_is_refused_before_the_tool_runs(self):
        response = self.protocol.handle(
            request("tools/call", {"name": "create_task", "arguments": {"title": "x"}})
        )
        self.assertEqual(response["error"]["code"], INVALID_PARAMS)
        self.assertIn("project_id", response["error"]["message"])
        self.assertIn("description", response["error"]["message"])
        self.assertEqual(self.vikunja.calls, [])

    def test_an_argument_of_the_wrong_shape_is_a_bad_request_not_a_crash(self):
        response = self.protocol.handle(
            request("tools/call", {"name": "get_task", "arguments": {"task_id": "nine"}})
        )
        self.assertEqual(response["error"]["code"], INVALID_PARAMS)

    def test_batches_are_refused(self):
        with self.assertRaises(JsonRpcError) as caught:
            self.protocol.handle([request("ping"), request("tools/list")])
        self.assertEqual(caught.exception.code, INVALID_REQUEST)

    def test_a_non_object_message_is_refused(self):
        with self.assertRaises(JsonRpcError) as caught:
            self.protocol.handle("tools/list")
        self.assertEqual(caught.exception.code, INVALID_REQUEST)

    def test_a_message_without_a_method_is_refused(self):
        with self.assertRaises(JsonRpcError) as caught:
            self.protocol.handle({"jsonrpc": "2.0", "id": 1})
        self.assertEqual(caught.exception.code, INVALID_REQUEST)


class TestNotifications(McpTestCase):
    def test_a_notification_gets_no_response(self):
        self.assertIsNone(
            self.protocol.handle(request("notifications/initialized", request_id=None))
        )

    def test_even_an_unknown_notification_gets_no_response(self):
        """The spec forbids answering a notification, including to complain."""
        self.assertIsNone(
            self.protocol.handle(request("nonsense/whatever", request_id=None))
        )


class TestToolResultShape(unittest.TestCase):
    """Built on stub tools so the shapes are checked without a Vikunja."""

    def protocol_for(self, run) -> McpProtocol:
        return McpProtocol(
            [
                Tool(
                    name="probe",
                    title="Probe",
                    description="test double",
                    input_schema={
                        "type": "object",
                        "properties": {},
                        "required": [],
                        "additionalProperties": False,
                    },
                    run=run,
                )
            ]
        )

    def test_a_result_carries_both_text_and_structured_content(self):
        protocol = self.protocol_for(lambda arguments: {"ok": True})
        result = protocol.handle(
            request("tools/call", {"name": "probe", "arguments": {}})
        )["result"]
        self.assertEqual(result["structuredContent"], {"ok": True})
        self.assertIn("ok", result["content"][0]["text"])
        self.assertNotIn("isError", result)

    def test_a_refusal_comes_back_as_a_flagged_result_the_model_can_read(self):
        """Not a transport error: the model is meant to see the reason."""

        def refuse(arguments):
            raise ToolError("nope, and here is why")

        protocol = self.protocol_for(refuse)
        response = protocol.handle(
            request("tools/call", {"name": "probe", "arguments": {}})
        )
        self.assertNotIn("error", response)
        self.assertTrue(response["result"]["isError"])
        self.assertIn("nope, and here is why", response["result"]["content"][0]["text"])

    def test_two_tools_with_one_name_is_a_construction_error(self):
        tool = Tool(
            name="probe",
            title="Probe",
            description="test double",
            input_schema={"type": "object", "properties": {}, "required": []},
            run=lambda arguments: None,
        )
        with self.assertRaises(ValueError):
            McpProtocol([tool, tool])


if __name__ == "__main__":
    unittest.main()
