"""Offline tests for loading accepted scripts."""

import copy
import unittest
from unittest.mock import Mock, patch

from cloud.acceptance import (
    build_briefing_acceptance,
    build_script_acceptance,
    load_accepted_script,
)


class AcceptedScriptLoadingTests(unittest.TestCase):
    def setUp(self):
        self.run_id = "2026-10-28-002"
        self.context = {
            "episode_date": "2026-10-28",
            "timezone": "America/Toronto",
            "as_of_timestamp": "2026-10-28T16:00:00-04:00",
            "run_id": self.run_id,
            "prompt_version": "v1",
        }
        self.script = (
            "HOST_A: " + " ".join(["word"] * 999)
            + "\nHOST_B: word"
        )
        briefing = {
            "briefing_markdown": "# Offline test briefing",
            "carry_forward": {
                "episode_date": self.context["episode_date"],
                "as_of": self.context["as_of_timestamp"],
                "topics": [],
            },
        }
        sources = [{"url": "https://example.com/offline-source"}]
        briefing_acceptance = build_briefing_acceptance(
            run_context=self.context,
            validated_result={
                "response_id": "resp_briefing_test",
                "model": "gpt-6.1-sol",
                "briefing": briefing,
                "sources": sources,
            },
        )
        script_inputs = {
            "run_context": self.context,
            "model": "gpt-6.1-sol",
            "briefing_response_id": "resp_briefing_test",
            "briefing_sha256": briefing_acceptance["briefing_sha256"],
        }
        script_acceptance = build_script_acceptance(
            script_inputs=script_inputs,
            validated_result={
                "response_id": "resp_script_test",
                "model": "gpt-6.1-sol",
                "script": self.script,
            },
            briefing_acceptance=briefing_acceptance,
        )

        self.files = {
            "inputs.json": {
                "run_context": self.context,
                "model": "gpt-6.1-sol",
            },
            "briefing_response_id.json": {
                "run_id": self.run_id,
                "response_id": "resp_briefing_test",
            },
            "briefing_validation.json": {
                "run_id": self.run_id,
                "response_id": "resp_briefing_test",
                "status": "validated",
                "source_count": 1,
            },
            "briefing.md": briefing["briefing_markdown"],
            "carry_forward.json": briefing["carry_forward"],
            "briefing_sources.json": sources,
            "briefing_acceptance.json": briefing_acceptance,
            "script_inputs.json": script_inputs,
            "script_response_id.json": {
                "run_id": self.run_id,
                "response_id": "resp_script_test",
            },
            "script.md": self.script,
            "script_acceptance.json": script_acceptance,
            "script_validation.json": {
                "run_id": self.run_id,
                "response_id": "resp_script_test",
                "model": "gpt-6.1-sol",
                "status": "validated",
                "script_sha256": script_acceptance["script_sha256"],
                "spoken_word_count": 1000,
                "dialogue_line_count": 2,
                "within_word_target": False,
                "acceptance_status": "requires_editorial_review",
                "audio_generation_allowed": False,
            },
        }
        self.storage = Mock()
        self.storage.read_json.side_effect = self.read
        self.storage.read_text.side_effect = self.read

    def read(self, run_id, filename):
        self.assertEqual(run_id, self.run_id)
        return copy.deepcopy(self.files[filename])

    def load(self):
        return load_accepted_script(
            run_id=self.run_id,
            storage=self.storage,
        )

    def test_returns_exact_text_despite_legacy_review_flags(self):
        before = copy.deepcopy(self.files)

        with patch(
            "cloud.prompt_loader.load_prompt_bundle",
            side_effect=AssertionError("Must not load current prompts"),
        ):
            result = self.load()

        self.assertEqual(result["script"], self.script)
        self.assertEqual(result["run_context"], self.context)
        self.assertEqual(result["response_id"], "resp_script_test")
        self.assertEqual(
            result["acceptance"],
            self.files["script_acceptance.json"],
        )
        self.assertEqual(self.files, before)
        self.storage.preserve_text.assert_not_called()
        self.storage.preserve_json.assert_not_called()

    def test_missing_artifacts_block_loading(self):
        for filename in list(self.files):
            with self.subTest(filename=filename):
                original = self.files.pop(filename)
                try:
                    with self.assertRaises(KeyError):
                        self.load()
                finally:
                    self.files[filename] = original

    def test_changed_script_including_outer_whitespace_is_rejected(self):
        for changed in (
            self.script.replace("word", "changed", 1),
            " " + self.script,
            self.script + "\n",
        ):
            with self.subTest(changed=repr(changed[:30])):
                self.files["script.md"] = changed
                with self.assertRaisesRegex(ValueError, "exact saved script"):
                    self.load()

    def test_mismatched_identity_and_evidence_are_rejected(self):
        cases = (
            ("script_inputs.json", "model", "different-model"),
            ("script_inputs.json", "briefing_response_id", "resp_other"),
            ("script_inputs.json", "briefing_sha256", "0" * 64),
            ("script_response_id.json", "run_id", "2026-10-28-003"),
            ("script_response_id.json", "response_id", "resp_other"),
            ("script_validation.json", "status", "pending"),
            ("script_validation.json", "run_id", "2026-10-28-003"),
            ("script_validation.json", "response_id", "resp_other"),
            ("script_validation.json", "model", "different-model"),
            ("script_validation.json", "script_sha256", "0" * 64),
            ("script_validation.json", "spoken_word_count", 999),
            ("script_validation.json", "dialogue_line_count", 3),
            ("script_validation.json", "within_word_target", True),
            ("script_acceptance.json", "status", "pending"),
            ("script_acceptance.json", "run_id", "2026-10-28-003"),
            ("script_acceptance.json", "policy_version", "unknown"),
            ("script_acceptance.json", "script_inputs_sha256", "0" * 64),
            ("script_acceptance.json", "briefing_acceptance_sha256", "0" * 64),
            ("script_acceptance.json", "eligible_uses", []),
            ("briefing_acceptance.json", "briefing_sha256", "0" * 64),
        )
        for filename, field, changed in cases:
            with self.subTest(filename=filename, field=field):
                original = copy.deepcopy(self.files[filename])
                try:
                    self.files[filename][field] = changed
                    with self.assertRaises(ValueError):
                        self.load()
                finally:
                    self.files[filename] = original

    def test_changed_briefing_dependency_is_rejected(self):
        self.files["briefing.md"] += "\nChanged content."
        with self.assertRaises(ValueError):
            self.load()

    def test_storage_failure_propagates(self):
        self.storage.read_text.side_effect = RuntimeError("Read failed")
        with self.assertRaisesRegex(RuntimeError, "Read failed"):
            self.load()


if __name__ == "__main__":
    unittest.main()