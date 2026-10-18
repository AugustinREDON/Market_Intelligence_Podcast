import copy
import json
import unittest

from cloud.output_validation import (
    validate_briefing_response,
    validate_script_response,
)


RUN_CONTEXT = {
    "episode_date": "2026-10-16",
    "timezone": "America/Toronto",
    "as_of_timestamp": "2026-10-16T06:00:00-04:00",
    "run_id": "2026-10-16-001",
    "prompt_version": "v1",
}

VALID_RESPONSE = {
    "briefing_markdown": "# Market Briefing\n\nExample briefing for testing.",
    "carry_forward": {
        "episode_date": "2026-10-16",
        "as_of": "2026-10-16T06:00:00-04:00",
        "topics": [
            {
                "topic": "Upcoming earnings",
                "status": "upcoming",
                "context": "Follow the next scheduled earnings release.",
            }
        ],
    },
}


class BriefingValidationTests(unittest.TestCase):
    def setUp(self):
        self.response = copy.deepcopy(VALID_RESPONSE)

    def validate(self):
        return validate_briefing_response(
            json.dumps(self.response),
            RUN_CONTEXT,
        )

    def test_valid_response(self):
        self.assertEqual(self.validate(), VALID_RESPONSE)

    def test_empty_topics_allowed(self):
        self.response["carry_forward"]["topics"] = []
        self.assertEqual(self.validate()["carry_forward"]["topics"], [])

    def test_invalid_raw_responses_rejected(self):
        valid_json = json.dumps(VALID_RESPONSE)

        invalid_responses = [
            "",
            "   ",
            "{broken json",
            f"```json\n{valid_json}\n```",
            f"Here is your briefing:\n{valid_json}",
            "[]",
            "null",
            '{"briefing_markdown": "First", "briefing_markdown": "Second"}',
        ]

        for raw_response in invalid_responses:
            with self.subTest(raw_response=raw_response):
                with self.assertRaises(ValueError):
                    validate_briefing_response(raw_response, RUN_CONTEXT)

    def test_empty_briefing_rejected(self):
        self.response["briefing_markdown"] = "   "
        with self.assertRaises(ValueError):
            self.validate()

    def test_missing_field_rejected(self):
        del self.response["carry_forward"]
        with self.assertRaises(ValueError):
            self.validate()

    def test_unexpected_field_rejected(self):
        self.response["summary"] = "An unexpected field."
        with self.assertRaises(ValueError):
            self.validate()

    def test_wrong_date_rejected(self):
        self.response["carry_forward"]["episode_date"] = "2026-10-15"
        with self.assertRaises(ValueError):
            self.validate()

    def test_wrong_cutoff_rejected(self):
        self.response["carry_forward"]["as_of"] = "2026-10-16T07:00:00-04:00"
        with self.assertRaises(ValueError):
            self.validate()

    def test_topics_must_be_list(self):
        self.response["carry_forward"]["topics"] = {}
        with self.assertRaises(ValueError):
            self.validate()

    def test_invalid_topic_status_rejected(self):
        self.response["carry_forward"]["topics"][0]["status"] = "finished"
        with self.assertRaises(ValueError):
            self.validate()

    def test_empty_topic_context_rejected(self):
        self.response["carry_forward"]["topics"][0]["context"] = ""
        with self.assertRaises(ValueError):
            self.validate()

    def test_missing_topic_field_rejected(self):
        del self.response["carry_forward"]["topics"][0]["topic"]
        with self.assertRaises(ValueError):
            self.validate()

class ScriptValidationTests(unittest.TestCase):
    def test_valid_dialogue(self):
        script = "HOST_A: Good morning.\nHOST_B: Welcome back."
        result = validate_script_response(script)

        self.assertEqual(result["script"], script)
        self.assertEqual(result["spoken_word_count"], 4)
        self.assertEqual(result["dialogue_line_count"], 2)
        self.assertFalse(result["within_word_target"])

    def test_blank_lines_allowed(self):
        script = "\nHOST_A: Hello.\n\nHOST_B: Welcome.\n"
        result = validate_script_response(script)

        self.assertEqual(result["dialogue_line_count"], 2)

    def test_consecutive_same_host_allowed(self):
        script = (
            "HOST_A: Hello.\n"
            "HOST_A: Here is today's news.\n"
            "HOST_B: Let's begin."
        )
        result = validate_script_response(script)

        self.assertEqual(result["dialogue_line_count"], 3)

    def test_invalid_scripts_rejected(self):
        invalid_scripts = {
            "empty": "",
            "whitespace": "   \n",
            "wrong_type": None,
            "missing_label": "Hello.\nHOST_B: Welcome.",
            "unknown_host": "HOST_A: Hello.\nHOST_C: Welcome.",
            "empty_dialogue": "HOST_A:   \nHOST_B: Welcome.",
            "wrong_first_host": "HOST_B: Hello.\nHOST_A: Welcome.",
            "missing_second_host": "HOST_A: Hello.",
            "heading": "# Podcast\nHOST_A: Hello.\nHOST_B: Welcome.",
            "code_fence": "```\nHOST_A: Hello.\nHOST_B: Welcome.\n```",
        }

        for name, script in invalid_scripts.items():
            with self.subTest(case=name):
                with self.assertRaises(ValueError):
                    validate_script_response(script)

    def test_word_target_boundaries(self):
        for word_count, expected in (
            (2199, False),
            (2200, True),
            (2800, True),
            (2801, False),
        ):
            with self.subTest(word_count=word_count):
                host_a_words = " ".join(["word"] * (word_count - 1))
                script = f"HOST_A: {host_a_words}\nHOST_B: word"

                result = validate_script_response(script)

                self.assertEqual(result["spoken_word_count"], word_count)
                self.assertEqual(result["within_word_target"], expected)

if __name__ == "__main__":
    unittest.main()