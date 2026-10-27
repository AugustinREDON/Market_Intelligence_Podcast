"""Offline tests for completed script response validation."""

import copy
import unittest
from unittest.mock import Mock

from cloud.text_generation import validate_completed_script


class ScriptResponseTests(unittest.TestCase):
    def setUp(self):
        self.response = {
            "id": "resp_script_test",
            "model": "gpt-6.1-sol",
            "status": "completed",
            "usage": {"input_tokens": 100, "output_tokens": 50},
            "output": [
                {
                    "type": "message",
                    "role": "assistant",
                    "status": "completed",
                    "content": [
                        {
                            "type": "output_text",
                            "text": (
                                "HOST_A: Welcome to the podcast.\n"
                                "HOST_B: Let's discuss the markets."
                            ),
                        }
                    ],
                }
            ],
        }

    def content(self):
        return self.response["output"][0]["content"]

    def test_valid_dialogue_preserves_metadata_and_flags_short_length(self):
        result = validate_completed_script(self.response)

        self.assertEqual(result["response_id"], "resp_script_test")
        self.assertEqual(result["model"], "gpt-6.1-sol")
        self.assertEqual(result["usage"], self.response["usage"])
        self.assertEqual(result["dialogue_line_count"], 2)
        self.assertFalse(result["within_word_target"])
        self.assertEqual(result["script"], self.content()[0]["text"])

    def test_word_target_boundaries(self):
        for count, expected in (
            (2199, False),
            (2200, True),
            (2800, True),
            (2801, False),
        ):
            with self.subTest(count=count):
                self.content()[0]["text"] = (
                    "HOST_A: "
                    + " ".join(["word"] * (count - 1))
                    + "\nHOST_B: Closing."
                )

                result = validate_completed_script(self.response)

                self.assertEqual(result["spoken_word_count"], count)
                self.assertEqual(result["within_word_target"], expected)

    def test_sdk_response_uses_public_json_dump(self):
        sdk_response = Mock()
        sdk_response.model_dump.return_value = copy.deepcopy(
            self.response
        )

        result = validate_completed_script(sdk_response)

        sdk_response.model_dump.assert_called_once_with(mode="json")
        self.assertEqual(result["response_id"], "resp_script_test")

    def test_unfinished_or_unsuccessful_responses_are_rejected(self):
        for status in (
            "queued",
            "in_progress",
            "failed",
            "cancelled",
            "incomplete",
        ):
            with self.subTest(status=status):
                self.response["status"] = status

                with self.assertRaisesRegex(ValueError, "not completed"):
                    validate_completed_script(self.response)

    def test_missing_response_identity_is_rejected(self):
        for field in ("id", "model"):
            with self.subTest(field=field):
                response = copy.deepcopy(self.response)
                del response[field]

                with self.assertRaises(ValueError):
                    validate_completed_script(response)

    def test_invalid_dialogue_is_rejected(self):
        for text in (
            "",
            "A heading without a host label",
            "HOST_B: Wrong first host.\nHOST_A: Hello.",
            "HOST_A: Only one host.",
            "HOST_A:\nHOST_B: Empty first turn.",
        ):
            with self.subTest(text=text):
                self.content()[0]["text"] = text

                with self.assertRaises(ValueError):
                    validate_completed_script(self.response)

    def test_refusals_and_tool_output_are_rejected(self):
        response = copy.deepcopy(self.response)
        response["output"][0]["content"] = [
            {"type": "refusal", "refusal": "Cannot generate."}
        ]

        with self.assertRaisesRegex(ValueError, "refusal"):
            validate_completed_script(response)

        response = copy.deepcopy(self.response)
        response["output"].append({
            "type": "web_search_call",
            "status": "completed",
        })

        with self.assertRaisesRegex(ValueError, "Unexpected"):
            validate_completed_script(response)

    def test_malformed_response_structure_is_rejected(self):
        variants = []

        for output in (None, {}, [None]):
            response = copy.deepcopy(self.response)
            response["output"] = output
            variants.append(response)

        for field, value in (
            ("role", "user"),
            ("status", "in_progress"),
            ("content", None),
            ("content", [None]),
            ("content", [{"type": "output_text", "text": 123}]),
        ):
            response = copy.deepcopy(self.response)
            response["output"][0][field] = value
            variants.append(response)

        for index, response in enumerate(variants):
            with self.subTest(variant=index):
                with self.assertRaises(ValueError):
                    validate_completed_script(response)


if __name__ == "__main__":
    unittest.main()