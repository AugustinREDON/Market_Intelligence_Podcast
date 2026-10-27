"""Offline tests for script request preparation."""

import json
import unittest
from unittest.mock import patch

from cloud.text_generation import build_script_request


class ScriptRequestTests(unittest.TestCase):
    def setUp(self):
        self.context = {
            "episode_date": "2026-10-26",
            "timezone": "America/Toronto",
            "as_of_timestamp": "2026-10-26T06:00:00-04:00",
            "run_id": "2026-10-26-001",
            "prompt_version": "v1",
        }
        self.briefing = (
            "# Market briefing\n\n"
            "First paragraph — preserve punctuation.\n\n"
            "Final paragraph: this must also reach the request."
        )
        self.arguments = {
            "model": "gpt-6.1-sol",
            "instructions": "Write HOST_A:/HOST_B: dialogue.",
            "run_context": self.context,
            "briefing_markdown": self.briefing,
        }

    def test_full_briefing_and_context_are_preserved(self):
        request = build_script_request(**self.arguments)
        _, separator, payload = request["input"].partition("\n")

        self.assertTrue(separator)
        supplied = json.loads(payload)

        self.assertEqual(supplied["briefing_markdown"], self.briefing)
        self.assertEqual(supplied["run_context"], self.context)
        self.assertEqual(supplied["episode_day_of_week"], "Monday")
        self.assertTrue(
            request["instructions"].startswith(
                self.arguments["instructions"]
            )
        )

    def test_request_has_no_research_or_briefing_schema(self):
        request = build_script_request(**self.arguments)

        self.assertEqual(request["model"], "gpt-6.1-sol")
        self.assertTrue(request["background"])
        self.assertTrue(request["store"])
        self.assertEqual(request["max_output_tokens"], 12_000)

        for field in (
            "tools",
            "tool_choice",
            "max_tool_calls",
            "include",
            "text",
            "previous_response_id",
        ):
            with self.subTest(field=field):
                self.assertNotIn(field, request)

    def test_missing_or_empty_required_text_is_rejected(self):
        for field in ("model", "instructions", "briefing_markdown"):
            for value in (None, "", "   "):
                with self.subTest(field=field, value=value):
                    arguments = {**self.arguments, field: value}

                    with self.assertRaises(ValueError):
                        build_script_request(**arguments)

    def test_invalid_context_is_rejected(self):
        arguments = {**self.arguments, "run_context": {}}

        with self.assertRaises(ValueError):
            build_script_request(**arguments)

    def test_building_does_not_initialize_an_api_client(self):
        with patch("cloud.text_generation.OpenAI") as client:
            build_script_request(**self.arguments)

        client.assert_not_called()

    def test_context_key_order_does_not_change_request(self):
        reversed_context = dict(
            reversed(list(self.context.items()))
        )

        first = build_script_request(**self.arguments)
        second = build_script_request(
            **{**self.arguments, "run_context": reversed_context}
        )

        self.assertEqual(first, second)


if __name__ == "__main__":
    unittest.main()