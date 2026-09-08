"""What `set_task_status` promises about the shape of its answer (task 853).

A tool may advertise an ``outputSchema`` beside its ``inputSchema``, and a
client that reads one validates ``structuredContent`` against it. Declaring one
is therefore a PROMISE about every successful answer, not documentation: a
shape the method can return and the schema does not describe is a client-side
failure on a call that worked.

So the schema is asserted against the ANSWERS, not against itself. Every test
below drives a real call through the protocol and validates the
``structuredContent`` a client would receive against the schema the same
protocol advertised in ``tools/list`` — served bytes on both sides, so a
constant edited without the method changing (or the reverse) fails here.

The validator is the small one in this file rather than a dependency: the
default run is stdlib-only. It implements exactly the keywords this schema
uses and RAISES on any other, so a keyword added to the schema that it cannot
enforce is a failure rather than a line it silently skips over.
"""

from __future__ import annotations

import copy
import unittest
from typing import Any

from .support import McpTestCase

#: The fixture's board number for a task sitting in Ready.
READY = 8

#: Every keyword the checker below implements. A schema using anything else is
#: refused: an unimplemented keyword is a constraint a reader believes in and
#: this file does not check.
SUPPORTED_KEYWORDS = {
    "type",
    "const",
    "description",
    "properties",
    "required",
    "additionalProperties",
    "oneOf",
}


class UnsupportedKeyword(Exception):
    """The schema constrains something this checker does not implement."""


def _is_type(value: Any, name: str) -> bool:
    # `bool` is an `int` in Python and is not one in JSON Schema, so the two
    # numeric types exclude it explicitly. Without that, `"changed": true`
    # would satisfy `{"type": "integer"}`.
    if name == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if name == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    return isinstance(
        value,
        {
            "object": dict,
            "array": list,
            "string": str,
            "boolean": bool,
            "null": type(None),
        }[name],
    )


def schema_errors(schema: dict[str, Any], value: Any, path: str = "$") -> list[str]:
    """Every way ``value`` fails ``schema``, or an empty list."""
    unsupported = set(schema) - SUPPORTED_KEYWORDS
    if unsupported:
        raise UnsupportedKeyword(f"{path}: {sorted(unsupported)}")

    failures: list[str] = []
    if "type" in schema and not _is_type(value, schema["type"]):
        # Nothing further is checkable once the type is wrong, and reporting
        # "missing 'bucket'" about a string would be noise, not a second fault.
        return [f"{path}: expected {schema['type']}, got {type(value).__name__}"]
    if "const" in schema and value != schema["const"]:
        failures.append(f"{path}: expected {schema['const']!r}, got {value!r}")

    if {"properties", "required", "additionalProperties"} & set(schema):
        if not isinstance(value, dict):
            return failures + [f"{path}: expected an object"]
        properties = schema.get("properties", {})
        failures += [
            f"{path}: missing {name!r}"
            for name in schema.get("required", ())
            if name not in value
        ]
        if schema.get("additionalProperties") is False:
            failures += [
                f"{path}: unexpected {name!r}"
                for name in value
                if name not in properties
            ]
        for name, subschema in properties.items():
            if name in value:
                failures += schema_errors(subschema, value[name], f"{path}.{name}")

    if "oneOf" in schema:
        matched = branches_matching(schema, value, path)
        if len(matched) != 1:
            failures.append(
                f"{path}: matched {len(matched)} of {len(schema['oneOf'])} "
                "branches, not exactly one"
            )
    return failures


def branches_matching(
    schema: dict[str, Any], value: Any, path: str = "$"
) -> list[int]:
    """Which ``oneOf`` branches ``value`` satisfies, by index."""
    return [
        index
        for index, branch in enumerate(schema["oneOf"])
        if not schema_errors(branch, value, path)
    ]


def walk_schemas(schema: Any):
    """Every subschema, so a keyword can be looked for at any depth."""
    if isinstance(schema, dict):
        yield schema
        for value in schema.get("properties", {}).values():
            yield from walk_schemas(value)
        for branch in schema.get("oneOf", ()):
            yield from walk_schemas(branch)


class OutputSchemaTestCase(McpTestCase):
    """Helpers shared by everything below: the served schema, and real answers."""

    def advertised(self, tool: str) -> dict[str, Any]:
        """One tool's descriptor, as `tools/list` publishes it."""
        response = self.protocol.handle(
            {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
        )
        assert response is not None
        listed = {t["name"]: t for t in response["result"]["tools"]}
        return listed[tool]

    def output_schema(self) -> dict[str, Any]:
        return self.advertised("set_task_status")["outputSchema"]

    def structured(self, **arguments) -> dict[str, Any]:
        """One `set_task_status` call, as a client sees the result."""
        result = self.call_tool("set_task_status", **arguments)["result"]
        self.assertNotIn("isError", result, result)
        return result["structuredContent"]

    def assertValidates(self, payload: dict[str, Any]) -> None:
        self.assertEqual(schema_errors(self.output_schema(), payload), [], payload)


class TestTheSchemaIsAdvertised(OutputSchemaTestCase):
    def test_set_task_status_publishes_an_output_schema(self):
        schema = self.output_schema()
        self.assertEqual(schema["type"], "object")
        self.assertFalse(schema["additionalProperties"])

    def test_it_uses_only_keywords_this_file_can_enforce(self):
        """An unenforceable keyword is a promise no test is keeping."""
        for subschema in walk_schemas(self.output_schema()):
            with self.subTest(keys=sorted(subschema)):
                self.assertEqual(set(subschema) - SUPPORTED_KEYWORDS, set())

    def test_a_tool_with_no_output_schema_omits_the_key(self):
        """Absent, not empty: an empty schema would promise a shape nobody set."""
        response = self.protocol.handle(
            {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
        )
        assert response is not None
        without = [
            tool["name"]
            for tool in response["result"]["tools"]
            if "outputSchema" not in tool
        ]
        self.assertIn("get_task", without)
        self.assertNotIn("set_task_status", without)

    def test_the_input_schema_is_untouched(self):
        """The output schema is an addition; the call contract is unchanged."""
        schema = self.advertised("set_task_status")["inputSchema"]
        self.assertEqual(schema["required"], ["task_number", "bucket"])
        self.assertEqual(
            set(schema["properties"]),
            {"task_number", "bucket", "approval_token", "project_id"},
        )
        self.assertFalse(schema["additionalProperties"])


class TestEveryAnswerValidates(OutputSchemaTestCase):
    """The three shapes `set_task_status` can return, each through the protocol."""

    def _approve(self, number: int, bucket: str) -> dict[str, Any]:
        preview = self.structured(task_number=number, bucket=bucket)
        return self.structured(
            task_number=number, bucket=bucket,
            approval_token=preview["approval_token"],
        )

    def test_the_task_is_already_in_that_column(self):
        payload = self.structured(task_number=READY, bucket="Ready")
        self.assertFalse(payload["changed"])
        self.assertIn("already", payload["reason"])
        self.assertValidates(payload)

    def test_a_preview_that_changed_nothing(self):
        payload = self.structured(task_number=READY, bucket="Done")
        self.assertTrue(payload["approval_required"])
        self.assertTrue(payload["approval_token"])
        self.assertValidates(payload)

    def test_a_completed_move(self):
        payload = self._approve(READY, "In Progress")
        self.assertTrue(payload["changed"])
        self.assertFalse(payload["reopened"])
        self.assertValidates(payload)

    def test_a_move_that_closes_the_ticket(self):
        payload = self._approve(READY, "Done")
        self.assertTrue(payload["done"])
        self.assertValidates(payload)

    def test_a_move_that_reopens_it(self):
        self._approve(READY, "Done")
        payload = self._approve(READY, "In Progress")
        self.assertTrue(payload["reopened"])
        self.assertValidates(payload)

    def test_each_answer_is_exactly_one_of_the_declared_shapes(self):
        """`oneOf`, not `anyOf`: a client can tell the three apart by shape."""
        schema = self.output_schema()
        answers = {
            "no-op": self.structured(task_number=READY, bucket="Ready"),
            "preview": self.structured(task_number=READY, bucket="Done"),
            "moved": self._approve(READY, "Waiting"),
        }
        seen = {}
        for name, payload in answers.items():
            with self.subTest(answer=name):
                matched = branches_matching(schema, payload)
                self.assertEqual(len(matched), 1, payload)
                seen[name] = matched[0]
        # Three answers, three different branches: none of the declared shapes
        # is unreachable, and no two answers collapse into one description.
        self.assertEqual(len(set(seen.values())), len(seen), seen)

    def test_a_refusal_carries_no_structured_content_to_validate(self):
        """The schema describes successes. A refusal is text, and says so."""
        result = self.call_tool(
            "set_task_status", task_number=READY, bucket="Reddy")["result"]
        self.assertTrue(result["isError"])
        self.assertNotIn("structuredContent", result)


class TestTheCheckBites(OutputSchemaTestCase):
    """The validator above, mutation-tested — an assertion that cannot fail is not one."""

    def _moved(self) -> dict[str, Any]:
        preview = self.structured(task_number=READY, bucket="Done")
        return self.structured(
            task_number=READY, bucket="Done",
            approval_token=preview["approval_token"],
        )

    def assertRejected(self, payload: dict[str, Any]) -> None:
        self.assertNotEqual(schema_errors(self.output_schema(), payload), [], payload)

    def test_an_extra_field_is_rejected(self):
        payload = self._moved()
        payload["vikunja_task_id"] = 649
        self.assertRejected(payload)

    def test_a_missing_field_is_rejected(self):
        payload = self._moved()
        del payload["reopened"]
        self.assertRejected(payload)

    def test_a_wrong_type_is_rejected(self):
        payload = self._moved()
        payload["task_number"] = str(payload["task_number"])
        self.assertRejected(payload)

    def test_a_boolean_does_not_pass_for_the_task_number(self):
        payload = self._moved()
        payload["task_number"] = True
        self.assertRejected(payload)

    def test_a_nested_field_is_checked(self):
        payload = self.structured(task_number=READY, bucket="Done")
        payload["current"]["done"] = "no"
        self.assertRejected(payload)

    def test_a_preview_that_also_claims_the_move_happened_is_rejected(self):
        """The branches are exclusive: a payload answering to two is not a shape."""
        payload = self.structured(task_number=READY, bucket="Done")
        payload.update(self._moved())
        self.assertRejected(payload)

    def test_a_shape_none_of_the_branches_describe_is_rejected(self):
        payload = self._moved()
        del payload["bucket"]
        del payload["done"]
        del payload["reopened"]
        self.assertRejected(payload)

    def test_a_keyword_the_checker_cannot_enforce_raises(self):
        schema = copy.deepcopy(self.output_schema())
        schema["properties"]["title"]["minLength"] = 1
        with self.assertRaises(UnsupportedKeyword):
            schema_errors(schema, self._moved())


if __name__ == "__main__":
    unittest.main()
