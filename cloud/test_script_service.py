"""Offline tests for script preparation."""

import copy
import hashlib
import json
import unittest
from unittest.mock import Mock, patch
from contextlib import nullcontext

from cloud.script_service import (
    load_prepared_script_run,
    prepare_script_run,
    retrieve_script_run,
    submit_prepared_script_run,
    validate_saved_script_run
)


class ScriptPreparationTests(unittest.TestCase):
    def setUp(self):
        self.run_id = "2026-10-26-001"
        self.context = {
            "episode_date": "2026-10-26",
            "timezone": "America/Toronto",
            "as_of_timestamp": "2026-10-26T06:00:00-04:00",
            "run_id": self.run_id,
            "prompt_version": "v1",
        }
        self.briefing = "# Briefing\n\nFull source text — final paragraph."
        self.files = {
            "inputs.json": {
                "run_context": self.context,
                "model": "gpt-6.1-sol",
            },
            "briefing_response_id.json": {
                "run_id": self.run_id,
                "response_id": "resp_test",
            },
            "briefing_validation.json": {
                "run_id": self.run_id,
                "response_id": "resp_test",
                "status": "validated",
                "source_count": 8,
            },
            "briefing.md": self.briefing,
            "carry_forward.json": {
                "episode_date": "2026-10-26",
                "as_of": "2026-10-26T06:00:00-04:00",
                "topics": [],
            },
        }

        self.storage = Mock()
        self.storage.read_json.side_effect = (
            lambda run_id, name: copy.deepcopy(self.files[name])
        )
        self.storage.read_text.side_effect = (
            lambda run_id, name: self.files[name]
        )
        self.storage.preserve_json.side_effect = self.preserve_json

        self.loader = patch(
            "cloud.script_service.load_prompt_bundle",
            return_value={"podcast": "Write host-labelled dialogue."},
        )
        self.loader.start()
        self.addCleanup(self.loader.stop)

    def preserve_json(self, run_id, name, value):
        self.assertEqual(run_id, self.run_id)

        if name in self.files:
            if self.files[name] != value:
                raise ValueError("Existing artifact differs")
            return "unchanged"

        self.files[name] = copy.deepcopy(value)
        return "created"

    def prepare(self):
        return prepare_script_run(
            run_id=self.run_id,
            model="gpt-6.1-sol",
            storage=self.storage,
        )

    def test_preserves_full_request_and_briefing_reference(self):
        result = self.prepare()
        request = self.files["script_request.json"]
        payload = json.loads(request["input"].split("\n", 1)[1])
        inputs = self.files["script_inputs.json"]

        self.assertFalse(result["generation_requested"])
        self.assertEqual(
            result["artifacts"],
            {
                "script_inputs.json": "created",
                "script_request.json": "created",
            },
        )
        self.assertEqual(payload["briefing_markdown"], self.briefing)
        self.assertEqual(payload["run_context"], self.context)
        self.assertEqual(inputs["briefing_response_id"], "resp_test")
        self.assertEqual(
            inputs["briefing_sha256"],
            hashlib.sha256(self.briefing.encode("utf-8")).hexdigest(),
        )

    def test_identical_preparation_is_safe_to_repeat(self):
        self.prepare()
        result = self.prepare()

        self.assertEqual(
            set(result["artifacts"].values()),
            {"unchanged"},
        )

    def test_missing_validation_marker_stops_before_writes(self):
        del self.files["briefing_validation.json"]

        with self.assertRaises(KeyError):
            self.prepare()

        self.storage.preserve_json.assert_not_called()

    def test_mismatched_validation_stops_before_writes(self):
        marker = self.files["briefing_validation.json"]

        for field, value in (
            ("run_id", "2026-10-25-001"),
            ("response_id", "resp_other"),
            ("status", "pending"),
            ("source_count", 0),
            ("source_count", True),
        ):
            with self.subTest(field=field, value=value):
                original = marker[field]
                marker[field] = value

                try:
                    with self.assertRaises(ValueError):
                        self.prepare()
                finally:
                    marker[field] = original

        self.storage.preserve_json.assert_not_called()

    def test_invalid_briefing_stops_before_writes(self):
        self.files["briefing.md"] = "   "

        with self.assertRaises(ValueError):
            self.prepare()

        self.storage.preserve_json.assert_not_called()

    def test_changed_briefing_cannot_replace_prepared_inputs(self):
        self.prepare()
        original_request = copy.deepcopy(
            self.files["script_request.json"]
        )
        self.files["briefing.md"] = "Different briefing content."

        with self.assertRaises(ValueError):
            self.prepare()

        self.assertEqual(
            self.files["script_request.json"],
            original_request,
        )

    def test_partial_write_can_be_recovered(self):
        def fail_request_write(run_id, name, value):
            if name == "script_request.json":
                raise RuntimeError("Simulated storage failure")
            return self.preserve_json(run_id, name, value)

        self.storage.preserve_json.side_effect = fail_request_write

        with self.assertRaises(RuntimeError):
            self.prepare()

        self.assertIn("script_inputs.json", self.files)
        self.assertNotIn("script_request.json", self.files)

        self.storage.preserve_json.side_effect = self.preserve_json
        result = self.prepare()

        self.assertEqual(
            result["artifacts"],
            {
                "script_inputs.json": "unchanged",
                "script_request.json": "created",
            },
        )

    def test_loader_returns_saved_request_without_writes(self):
        self.prepare()
        saved = copy.deepcopy(self.files["script_request.json"])
        self.storage.preserve_json.reset_mock()

        result = load_prepared_script_run(
            run_id=self.run_id,
            storage=self.storage,
        )

        self.assertEqual(result["request"], saved)
        self.assertEqual(result["run_context"], self.context)
        self.storage.preserve_json.assert_not_called()

    def test_loader_rejects_changed_request(self):
        self.prepare()
        self.files["script_request.json"]["max_output_tokens"] = 100

        with self.assertRaisesRegex(ValueError, "Saved script request"):
            load_prepared_script_run(
                run_id=self.run_id,
                storage=self.storage,
            )

    def test_loader_rejects_changed_briefing(self):
        self.prepare()
        self.files["briefing.md"] = "Different briefing content."

        with self.assertRaisesRegex(ValueError, "Saved script inputs"):
            load_prepared_script_run(
                run_id=self.run_id,
                storage=self.storage,
            )

    def test_loader_rejects_changed_prompt(self):
        self.prepare()

        with patch(
            "cloud.script_service.load_prompt_bundle",
            return_value={"podcast": "Changed podcast instructions."},
        ):
            with self.assertRaisesRegex(
                ValueError,
                "Saved script request",
            ):
                load_prepared_script_run(
                    run_id=self.run_id,
                    storage=self.storage,
                )

    def make_submission_client(self):
        self.prepare()
        self.storage.claim_script_submission.side_effect = [True, False]

        client = Mock()
        client.responses.create.return_value = {
            "id": "resp_script_test",
            "status": "queued",
        }
        factory = Mock(side_effect=lambda: nullcontext(client))
        return client, factory

    def test_submission_sends_saved_request_and_preserves_response(self):
        client, factory = self.make_submission_client()
        saved = copy.deepcopy(self.files["script_request.json"])

        result = submit_prepared_script_run(
            run_id=self.run_id,
            storage=self.storage,
            client_factory=factory,
        )

        client.responses.create.assert_called_once_with(**saved)
        self.assertTrue(result["generation_requested"])
        self.assertEqual(
            self.files["script_response_id.json"],
            {
                "run_id": self.run_id,
                "response_id": "resp_script_test",
            },
        )
        self.assertEqual(
            self.files["script_response_initial.json"]["status"],
            "queued",
        )

    def test_duplicate_submission_is_blocked(self):
        client, factory = self.make_submission_client()

        first = submit_prepared_script_run(
            run_id=self.run_id,
            storage=self.storage,
            client_factory=factory,
        )
        second = submit_prepared_script_run(
            run_id=self.run_id,
            storage=self.storage,
            client_factory=factory,
        )

        self.assertTrue(first["generation_requested"])
        self.assertFalse(second["generation_requested"])
        self.assertEqual(
            second["status"],
            "existing_claim_submission_blocked",
        )
        client.responses.create.assert_called_once()

    def test_changed_request_blocks_client_and_claim(self):
        client, factory = self.make_submission_client()
        self.files["script_request.json"]["max_output_tokens"] = 100

        with self.assertRaises(ValueError):
            submit_prepared_script_run(
                run_id=self.run_id,
                storage=self.storage,
                client_factory=factory,
            )

        factory.assert_not_called()
        self.storage.claim_script_submission.assert_not_called()
        client.responses.create.assert_not_called()

    def test_client_failure_leaves_submission_unclaimed(self):
        client, factory = self.make_submission_client()
        factory.side_effect = RuntimeError("Client initialization failed")

        with self.assertRaises(RuntimeError):
            submit_prepared_script_run(
                run_id=self.run_id,
                storage=self.storage,
                client_factory=factory,
            )

        self.storage.claim_script_submission.assert_not_called()
        client.responses.create.assert_not_called()

    def test_claim_failure_prevents_api_submission(self):
        client, factory = self.make_submission_client()
        self.storage.claim_script_submission.side_effect = RuntimeError(
            "Claim storage failed"
        )

        with self.assertRaises(RuntimeError):
            submit_prepared_script_run(
                run_id=self.run_id,
                storage=self.storage,
                client_factory=factory,
            )

        client.responses.create.assert_not_called()

    def test_submission_timeout_is_not_retried(self):
        client, factory = self.make_submission_client()
        client.responses.create.side_effect = TimeoutError()

        with self.assertRaises(TimeoutError):
            submit_prepared_script_run(
                run_id=self.run_id,
                storage=self.storage,
                client_factory=factory,
            )

        evidence = self.files["script_submission_failure.json"]
        self.assertEqual(evidence["stage"], "openai_submission")
        self.assertFalse(evidence["automatic_resubmission_allowed"])

        # The retained claim blocks a subsequent attempt.
        result = submit_prepared_script_run(
            run_id=self.run_id,
            storage=self.storage,
            client_factory=factory,
        )
        self.assertFalse(result["generation_requested"])
        client.responses.create.assert_called_once()

    def test_response_storage_failure_preserves_recovery_reference(self):
        client, factory = self.make_submission_client()

        def fail_initial_response(run_id, name, value):
            if name == "script_response_initial.json":
                raise RuntimeError("Initial response storage failed")
            return self.preserve_json(run_id, name, value)

        self.storage.preserve_json.side_effect = fail_initial_response

        with self.assertRaises(RuntimeError):
            submit_prepared_script_run(
                run_id=self.run_id,
                storage=self.storage,
                client_factory=factory,
            )

        self.assertEqual(
            self.files["script_response_id.json"]["response_id"],
            "resp_script_test",
        )
        evidence = self.files["script_submission_failure.json"]
        self.assertEqual(evidence["stage"], "initial_response_storage")
        self.assertEqual(evidence["response_id"], "resp_script_test")
        self.assertFalse(evidence["automatic_resubmission_allowed"])
        client.responses.create.assert_called_once()

    def make_retrieval_client(self, status="completed"):
        self.files["script_response_id.json"] = {
            "run_id": self.run_id,
            "response_id": "resp_script_test",
        }
        client = Mock()
        client.responses.retrieve.return_value = {
            "id": "resp_script_test",
            "status": status,
        }
        factory = Mock(side_effect=lambda: nullcontext(client))
        return client, factory

    def test_retrieval_preserves_completed_response_without_submission(self):
        client, factory = self.make_retrieval_client()

        result = retrieve_script_run(
            run_id=self.run_id,
            storage=self.storage,
            client_factory=factory,
        )

        self.assertEqual(result["status"], "completed_response_preserved")
        self.assertFalse(result["generation_requested"])
        self.assertEqual(
            self.files[result["snapshot_filename"]],
            client.responses.retrieve.return_value,
        )
        client.responses.retrieve.assert_called_once_with(
            "resp_script_test"
        )
        client.responses.create.assert_not_called()

    def test_retrieval_reports_pending_and_unsuccessful_statuses(self):
        for status, expected in (
            ("queued", "generation_pending"),
            ("in_progress", "generation_pending"),
            ("failed", "generation_unsuccessful"),
            ("cancelled", "generation_unsuccessful"),
            ("incomplete", "generation_unsuccessful"),
        ):
            with self.subTest(status=status):
                client, factory = self.make_retrieval_client(status)

                result = retrieve_script_run(
                    run_id=self.run_id,
                    storage=self.storage,
                    client_factory=factory,
                )

                self.assertEqual(result["status"], expected)
                self.assertIn(result["snapshot_filename"], self.files)
                client.responses.create.assert_not_called()

    def test_repeated_retrieval_preserves_separate_snapshots(self):
        client, factory = self.make_retrieval_client()

        first = retrieve_script_run(
            run_id=self.run_id,
            storage=self.storage,
            client_factory=factory,
        )
        second = retrieve_script_run(
            run_id=self.run_id,
            storage=self.storage,
            client_factory=factory,
        )

        self.assertNotEqual(
            first["snapshot_filename"],
            second["snapshot_filename"],
        )
        self.assertIn(first["snapshot_filename"], self.files)
        self.assertIn(second["snapshot_filename"], self.files)
        client.responses.create.assert_not_called()

    def test_invalid_retrieval_reference_blocks_client(self):
        client, factory = self.make_retrieval_client()
        self.files["script_response_id.json"]["run_id"] = "wrong_run"

        with self.assertRaises(ValueError):
            retrieve_script_run(
                run_id=self.run_id,
                storage=self.storage,
                client_factory=factory,
            )

        factory.assert_not_called()
        client.responses.create.assert_not_called()

    def test_wrong_retrieval_id_is_preserved_then_rejected(self):
        client, factory = self.make_retrieval_client()
        client.responses.retrieve.return_value["id"] = "resp_wrong"

        with self.assertRaisesRegex(ValueError, "does not match"):
            retrieve_script_run(
                run_id=self.run_id,
                storage=self.storage,
                client_factory=factory,
            )

        snapshots = [
            value
            for name, value in self.files.items()
            if name.startswith("script_response_snapshot_")
        ]
        self.assertEqual(len(snapshots), 1)
        self.assertEqual(snapshots[0]["id"], "resp_wrong")
        client.responses.create.assert_not_called()

    def test_retrieval_failure_does_not_submit(self):
        client, factory = self.make_retrieval_client()
        client.responses.retrieve.side_effect = TimeoutError()

        with self.assertRaises(TimeoutError):
            retrieve_script_run(
                run_id=self.run_id,
                storage=self.storage,
                client_factory=factory,
            )

        client.responses.retrieve.assert_called_once()
        client.responses.create.assert_not_called()

    def test_retrieval_storage_failure_is_not_reported_as_success(self):
        client, factory = self.make_retrieval_client()
        self.storage.preserve_json.side_effect = RuntimeError(
            "Snapshot storage failed"
        )

        with self.assertRaises(RuntimeError):
            retrieve_script_run(
                run_id=self.run_id,
                storage=self.storage,
                client_factory=factory,
            )

        client.responses.create.assert_not_called()

    def make_validation_snapshot(self):
        self.prepare()
        self.snapshot_filename = "script_response_snapshot_offline.json"

        self.files["script_response_id.json"] = {
            "run_id": self.run_id,
            "response_id": "resp_script_test",
        }
        self.files[self.snapshot_filename] = {
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
        self.storage.preserve_text.side_effect = (
            lambda run_id, name, text, **kwargs:
            self.preserve_json(run_id, name, text)
        )
        self.storage.preserve_json.reset_mock()

    def validate_snapshot(self):
        return validate_saved_script_run(
            run_id=self.run_id,
            snapshot_filename=self.snapshot_filename,
            storage=self.storage,
        )

    def test_validation_extracts_script_usage_and_review_marker(self):
        self.make_validation_snapshot()

        with patch("cloud.script_service.create_client") as client:
            result = self.validate_snapshot()

        client.assert_not_called()
        self.assertIn("script.md", self.files)
        self.assertEqual(
            self.files["script_usage.json"]["response_id"],
            "resp_script_test",
        )

        marker = self.files["script_validation.json"]
        self.assertEqual(marker["status"], "validated")
        self.assertEqual(
            marker["acceptance_status"],
            "requires_editorial_review",
        )
        self.assertFalse(marker["audio_generation_allowed"])
        self.assertFalse(marker["within_word_target"])
        self.assertFalse(result["generation_requested"])

        self.assertEqual(
            self.storage.preserve_json.call_args_list[-1].args[1],
            "script_validation.json",
        )

    def test_validation_rejects_wrong_response_identity(self):
        self.make_validation_snapshot()
        original = copy.deepcopy(self.files[self.snapshot_filename])

        for field, value in (
            ("id", "resp_wrong"),
            ("model", "different-model"),
        ):
            with self.subTest(field=field):
                self.files[self.snapshot_filename] = {
                    **copy.deepcopy(original),
                    field: value,
                }

                with self.assertRaises(ValueError):
                    self.validate_snapshot()

        self.storage.preserve_text.assert_not_called()
        self.storage.preserve_json.assert_not_called()

    def test_validation_rejects_pending_and_invalid_dialogue(self):
        self.make_validation_snapshot()
        original = copy.deepcopy(self.files[self.snapshot_filename])

        pending = copy.deepcopy(original)
        pending["status"] = "in_progress"

        invalid = copy.deepcopy(original)
        invalid["output"][0]["content"][0]["text"] = "Invalid dialogue"

        for snapshot in (pending, invalid):
            with self.subTest(status=snapshot["status"]):
                self.files[self.snapshot_filename] = snapshot

                with self.assertRaises(ValueError):
                    self.validate_snapshot()

        self.storage.preserve_text.assert_not_called()
        self.storage.preserve_json.assert_not_called()

    def test_validation_can_be_repeated_safely(self):
        self.make_validation_snapshot()
        self.validate_snapshot()

        result = self.validate_snapshot()

        self.assertEqual(
            set(result["artifacts"].values()),
            {"unchanged"},
        )

    def test_validation_recovers_from_partial_extraction(self):
        self.make_validation_snapshot()

        def fail_usage_write(run_id, name, value):
            if name == "script_usage.json":
                raise RuntimeError("Usage storage failed")
            return self.preserve_json(run_id, name, value)

        self.storage.preserve_json.side_effect = fail_usage_write

        with self.assertRaises(RuntimeError):
            self.validate_snapshot()

        self.assertIn("script.md", self.files)
        self.assertNotIn("script_validation.json", self.files)

        self.storage.preserve_json.side_effect = self.preserve_json
        result = self.validate_snapshot()

        self.assertEqual(
            result["artifacts"],
            {
                "script.md": "unchanged",
                "script_usage.json": "created",
                "script_validation.json": "created",
            },
        )

    def test_validation_does_not_reload_current_prompt(self):
        self.make_validation_snapshot()

        with patch(
            "cloud.script_service.load_prompt_bundle",
            side_effect=RuntimeError("Prompt must not be loaded"),
        ):
            result = self.validate_snapshot()

        self.assertEqual(
            result["status"],
            "script_validated_and_preserved",
        )


if __name__ == "__main__":
    unittest.main()