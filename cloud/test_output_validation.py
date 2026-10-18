import copy
import json
import unittest

from cloud.output_validation import validate_briefing_response


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


if __name__ == "__main__":
    unittest.main()