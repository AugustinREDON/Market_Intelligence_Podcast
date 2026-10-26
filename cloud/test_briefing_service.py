"""Offline tests for the cloud briefing submission flow."""

import unittest
import json
from unittest.mock import MagicMock, Mock, patch

from cloud.briefing_service import (
    retrieve_briefing_run,
    submit_prepared_briefing_run,
    validate_saved_briefing_run,
)
from cloud.storage import S3RunStorage
from cloud.test_storage import FakeS3
from cloud.text_generation import build_briefing_request


class BriefingSubmissionTests(unittest.TestCase):
    def setUp(self):
        self.run_id = "2026-10-25-996"
        self.run_context = {
            "episode_date": "2026-10-25",
            "timezone": "America/Toronto",
            "as_of_timestamp": "2026-10-25T06:00:00-04:00",
            "run_id": self.run_id,
            "prompt_version": "v1",
        }

        self.storage = S3RunStorage(
            bucket="offline-test",
            client=FakeS3(),
        )

        self.instructions = "Research an offline test briefing."
        self.request = build_briefing_request(
            model="gpt-6.1-sol",
            instructions=self.instructions,
            run_context=self.run_context,
            prior_carry_forward=None,
        )

        self.storage.preserve_json(
            self.run_id,
            "inputs.json",
            {
                "run_context": self.run_context,
                "model": "gpt-6.1-sol",
                "prior_carry_forward": None,
                "continuity_mode": "explicit_without_prior_state",
            },
        )
        self.storage.preserve_json(
            self.run_id,
            "request.json",
            self.request,
        )

        self.response = {
            "id": "resp_offline_test",
            "status": "queued",
            "model": "gpt-6.1-sol",
            "output": [],
        }

        self.client = Mock()
        self.client.responses.create.return_value = self.response

        self.client_context = MagicMock()
        self.client_context.__enter__.return_value = self.client
        self.client_context.__exit__.return_value = False
        self.client_factory = Mock(return_value=self.client_context)

        # Supply a tiny prompt instead of reading production prompt files.
        prompt_patch = patch(
            "cloud.briefing_service.load_prompt_bundle",
            return_value={"briefing": self.instructions},
        )
        prompt_patch.start()
        self.addCleanup(prompt_patch.stop)

    def submit(self):
        return submit_prepared_briefing_run(
            run_id=self.run_id,
            storage=self.storage,
            client_factory=self.client_factory,
        )

    def test_submits_exact_saved_request_and_preserves_response(self):
        result = self.submit()

        self.client.responses.create.assert_called_once_with(
            **self.request
        )
        self.assertEqual(result["status"], "initial_response_preserved")
        self.assertTrue(result["generation_requested"])

        saved_id = self.storage.read_json(
            self.run_id,
            "briefing_response_id.json",
        )
        self.assertEqual(saved_id["response_id"], self.response["id"])
        self.assertEqual(
            self.storage.read_json(
                self.run_id,
                "briefing_response_initial.json",
            ),
            self.response,
        )

    def test_second_attempt_does_not_submit_again(self):
        self.submit()
        result = self.submit()

        self.client.responses.create.assert_called_once()
        self.assertEqual(
            result["status"],
            "existing_claim_submission_blocked",
        )
        self.assertFalse(result["generation_requested"])

    def test_changed_request_is_rejected_before_claim(self):
        key = (
            self.storage.bucket,
            f"runs/{self.run_id}/request.json",
        )
        # Simulate a mismatched saved request inside fake storage only.
        self.storage.client.objects[key] = b'{"model": "different"}'

        with self.assertRaisesRegex(
            ValueError,
            "Saved request input has an unexpected format",
        ):
            self.submit()

        self.client_factory.assert_not_called()
        self.client.responses.create.assert_not_called()
        self.assertTrue(
            self.storage.claim_briefing_submission(self.run_id)
        )

    def test_read_failure_stops_before_client_or_claim(self):
        with patch.object(
            self.storage,
            "read_json",
            side_effect=RuntimeError("Simulated read failure"),
        ):
            with self.assertRaises(RuntimeError):
                self.submit()

        self.client_factory.assert_not_called()
        self.assertTrue(
            self.storage.claim_briefing_submission(self.run_id)
        )

    def test_client_initialization_failure_leaves_run_unclaimed(self):
        self.client_factory.side_effect = RuntimeError(
            "Simulated client initialization failure"
        )

        with self.assertRaises(RuntimeError):
            self.submit()

        self.client.responses.create.assert_not_called()
        self.assertTrue(
            self.storage.claim_briefing_submission(self.run_id)
        )

    def test_claim_failure_prevents_submission(self):
        with patch.object(
            self.storage,
            "claim_briefing_submission",
            side_effect=RuntimeError("Simulated claim failure"),
        ):
            with self.assertRaises(RuntimeError):
                self.submit()

        self.client.responses.create.assert_not_called()

    def test_submission_failure_keeps_claim_and_records_evidence(self):
        self.client.responses.create.side_effect = TimeoutError(
            "Simulated uncertain acceptance"
        )

        with self.assertRaises(TimeoutError):
            self.submit()

        evidence = self.storage.read_json(
            self.run_id,
            "briefing_submission_failure.json",
        )
        self.assertEqual(evidence["stage"], "openai_submission")
        self.assertFalse(evidence["automatic_resubmission_allowed"])

        result = self.submit()

        self.client.responses.create.assert_called_once()
        self.assertFalse(result["generation_requested"])

    def test_response_storage_failure_keeps_claim_and_response_id(self):
        original_preserve = self.storage.preserve_json

        def fail_initial_response(run_id, filename, value):
            if filename == "briefing_response_initial.json":
                raise RuntimeError("Simulated response storage failure")
            return original_preserve(run_id, filename, value)

        with patch.object(
            self.storage,
            "preserve_json",
            side_effect=fail_initial_response,
        ):
            with self.assertRaises(RuntimeError):
                self.submit()

        saved_id = self.storage.read_json(
            self.run_id,
            "briefing_response_id.json",
        )
        self.assertEqual(saved_id["response_id"], self.response["id"])

        evidence = self.storage.read_json(
            self.run_id,
            "briefing_submission_failure.json",
        )
        self.assertEqual(evidence["stage"], "initial_response_storage")
        self.assertEqual(evidence["response_id"], self.response["id"])

        result = self.submit()

        self.client.responses.create.assert_called_once()
        self.assertFalse(result["generation_requested"])

class BriefingRetrievalTests(unittest.TestCase):
    def setUp(self):
        self.run_id = "2026-10-25-995"
        self.response_id = "resp_offline_retrieval"
        self.storage = S3RunStorage(
            bucket="offline-test",
            client=FakeS3(),
        )
        self.storage.preserve_json(
            self.run_id,
            "briefing_response_id.json",
            {
                "run_id": self.run_id,
                "response_id": self.response_id,
            },
        )

        self.response = {
            "id": self.response_id,
            "status": "completed",
            "output": [],
        }
        self.client = Mock()
        self.client.responses.retrieve.return_value = self.response

        client_context = MagicMock()
        client_context.__enter__.return_value = self.client
        client_context.__exit__.return_value = False
        self.client_factory = Mock(return_value=client_context)

    def retrieve(self):
        return retrieve_briefing_run(
            run_id=self.run_id,
            storage=self.storage,
            client_factory=self.client_factory,
        )

    def test_completed_response_is_preserved_without_submission(self):
        result = self.retrieve()

        self.client.responses.retrieve.assert_called_once_with(
            self.response_id
        )
        self.client.responses.create.assert_not_called()
        self.assertEqual(
            result["status"], "completed_response_preserved"
        )
        self.assertFalse(result["generation_requested"])
        self.assertEqual(
            self.storage.read_json(
                self.run_id, result["snapshot_filename"]
            ),
            self.response,
        )

    def test_pending_statuses_are_reported(self):
        for status in ("queued", "in_progress"):
            with self.subTest(status=status):
                self.response["status"] = status
                result = self.retrieve()
                self.assertEqual(result["status"], "generation_pending")
                self.assertEqual(result["response_status"], status)

        self.client.responses.create.assert_not_called()

    def test_unsuccessful_statuses_are_preserved(self):
        for status in ("failed", "cancelled", "incomplete"):
            with self.subTest(status=status):
                self.response["status"] = status
                result = self.retrieve()
                self.assertEqual(
                    result["status"], "generation_unsuccessful"
                )
                self.assertEqual(
                    self.storage.read_json(
                        self.run_id, result["snapshot_filename"]
                    ),
                    self.response,
                )

    def test_repeated_retrieval_preserves_separate_snapshots(self):
        self.response["status"] = "in_progress"
        first = self.retrieve()

        self.response["status"] = "completed"
        second = self.retrieve()

        self.assertNotEqual(
            first["snapshot_filename"], second["snapshot_filename"]
        )
        self.assertEqual(
            self.storage.read_json(
                self.run_id, first["snapshot_filename"]
            )["status"],
            "in_progress",
        )
        self.assertEqual(
            self.storage.read_json(
                self.run_id, second["snapshot_filename"]
            )["status"],
            "completed",
        )
        self.client.responses.create.assert_not_called()

    def test_invalid_reference_stops_before_api_call(self):
        with patch.object(
            self.storage,
            "read_json",
            return_value={
                "run_id": "different-run",
                "response_id": self.response_id,
            },
        ):
            with self.assertRaisesRegex(ValueError, "different run_id"):
                self.retrieve()

        self.client_factory.assert_not_called()

    def test_retrieval_failure_does_not_submit(self):
        self.client.responses.retrieve.side_effect = TimeoutError(
            "Simulated retrieval timeout"
        )

        with self.assertRaises(TimeoutError):
            self.retrieve()

        self.client.responses.create.assert_not_called()

    def test_snapshot_storage_failure_is_not_reported_as_success(self):
        with patch.object(
            self.storage,
            "preserve_json",
            side_effect=RuntimeError("Simulated storage failure"),
        ):
            with self.assertRaises(RuntimeError):
                self.retrieve()

        self.client.responses.create.assert_not_called()

    def test_wrong_response_id_is_preserved_then_rejected(self):
        self.response["id"] = "resp_wrong"

        with self.assertRaisesRegex(ValueError, "does not match"):
            self.retrieve()

        snapshots = [
            key
            for key in self.storage.client.objects
            if "briefing_response_snapshot_" in key[1]
        ]
        self.assertEqual(len(snapshots), 1)
        filename = snapshots[0][1].rsplit("/", 1)[1]
        self.assertEqual(
            self.storage.read_json(self.run_id, filename)["id"],
            "resp_wrong",
        )
        self.client.responses.create.assert_not_called()

class BriefingValidationTests(unittest.TestCase):
    def setUp(self):
        self.run_id = "2026-10-25-994"
        self.response_id = "resp_offline_validation"
        self.filename = "briefing_response_snapshot_offline.json"
        self.storage = S3RunStorage(
            bucket="offline-test",
            client=FakeS3(),
        )
        self.run_context = {
            "episode_date": "2026-10-25",
            "timezone": "America/Toronto",
            "as_of_timestamp": "2026-10-25T06:00:00-04:00",
            "run_id": self.run_id,
            "prompt_version": "v1",
        }
        self.briefing = {
            "briefing_markdown": "# Test briefing\nMarkets were mixed.",
            "carry_forward": {
                "episode_date": "2026-10-25",
                "as_of": "2026-10-25T06:00:00-04:00",
                "topics": [],
            },
        }
        self.snapshot = {
            "id": self.response_id,
            "model": "gpt-6.1-sol",
            "status": "completed",
            "usage": {
                "input_tokens": 100,
                "output_tokens": 50,
                "total_tokens": 150,
            },
            "output": [
                {
                    "type": "web_search_call",
                    "status": "completed",
                    "action": {
                        "sources": [
                            {
                                "url": "https://example.com/markets",
                                "title": "Offline source",
                            }
                        ]
                    },
                },
                {
                    "type": "message",
                    "content": [
                        {
                            "type": "output_text",
                            "text": json.dumps(self.briefing),
                            "annotations": [],
                        }
                    ],
                },
            ],
        }
        self.storage.preserve_json(
            self.run_id,
            "inputs.json",
            {
                "run_context": self.run_context,
                "model": "gpt-6.1-sol",
                "prior_carry_forward": None,
                "continuity_mode": "explicit_without_prior_state",
            },
        )
        self.storage.preserve_json(
            self.run_id,
            "briefing_response_id.json",
            {
                "run_id": self.run_id,
                "response_id": self.response_id,
            },
        )

    def validate(self):
        return validate_saved_briefing_run(
            run_id=self.run_id,
            snapshot_filename=self.filename,
            storage=self.storage,
        )

    def save_snapshot(self):
        self.storage.preserve_json(
            self.run_id, self.filename, self.snapshot
        )

    def assert_no_extracted_artifacts(self):
        for filename in (
            "briefing.md",
            "carry_forward.json",
            "briefing_sources.json",
            "briefing_usage.json",
            "briefing_validation.json",
        ):
            self.assertNotIn(
                (
                    self.storage.bucket,
                    f"runs/{self.run_id}/{filename}",
                ),
                self.storage.client.objects,
            )

    def test_valid_response_extracts_artifacts_without_api_client(self):
        self.save_snapshot()

        with patch("cloud.briefing_service.create_client") as factory:
            result = self.validate()

        factory.assert_not_called()
        self.assertEqual(
            result["status"], "briefing_validated_and_preserved"
        )
        self.assertEqual(result["source_count"], 1)
        self.assertEqual(
            self.storage.read_text(self.run_id, "briefing.md"),
            self.briefing["briefing_markdown"],
        )
        self.assertEqual(
            self.storage.read_json(self.run_id, "carry_forward.json"),
            self.briefing["carry_forward"],
        )
        self.assertEqual(
            self.storage.read_json(
                self.run_id, "briefing_usage.json"
            )["usage"],
            self.snapshot["usage"],
        )
        self.assertEqual(
            self.storage.read_json(
                self.run_id, "briefing_validation.json"
            )["status"],
            "validated",
        )
        self.assertTrue(
            all(value == "created" for value in result["artifacts"].values())
        )

    def test_reprocessing_identical_snapshot_is_safe(self):
        self.save_snapshot()
        self.validate()
        result = self.validate()

        self.assertTrue(
            all(value == "unchanged" for value in result["artifacts"].values())
        )

    def test_pending_response_is_rejected_before_extraction(self):
        self.snapshot["status"] = "in_progress"
        self.save_snapshot()

        with self.assertRaisesRegex(ValueError, "not completed"):
            self.validate()

        self.assert_no_extracted_artifacts()

    def test_wrong_response_id_is_rejected_before_extraction(self):
        self.snapshot["id"] = "resp_wrong"
        self.save_snapshot()

        with self.assertRaisesRegex(ValueError, "does not match"):
            self.validate()

        self.assert_no_extracted_artifacts()

    def test_missing_search_evidence_is_rejected(self):
        self.snapshot["output"] = self.snapshot["output"][1:]
        self.save_snapshot()

        with self.assertRaisesRegex(ValueError, "No completed web-search"):
            self.validate()

        self.assert_no_extracted_artifacts()

    def test_invalid_briefing_is_rejected_before_extraction(self):
        self.briefing["carry_forward"]["episode_date"] = "2026-10-24"
        self.snapshot["output"][1]["content"][0]["text"] = json.dumps(
            self.briefing
        )
        self.save_snapshot()

        with self.assertRaisesRegex(ValueError, "episode_date must match"):
            self.validate()

        self.assert_no_extracted_artifacts()

    def test_partial_storage_failure_can_be_reprocessed(self):
        self.save_snapshot()
        original_preserve = self.storage.preserve_json

        def fail_sources(run_id, filename, value):
            if filename == "briefing_sources.json":
                raise RuntimeError("Simulated storage failure")
            return original_preserve(run_id, filename, value)

        with patch.object(
            self.storage, "preserve_json", side_effect=fail_sources
        ):
            with self.assertRaises(RuntimeError):
                self.validate()

        self.assertNotIn(
            (
                self.storage.bucket,
                f"runs/{self.run_id}/briefing_validation.json",
            ),
            self.storage.client.objects,
        )

        result = self.validate()

        self.assertEqual(result["artifacts"]["briefing.md"], "unchanged")
        self.assertEqual(
            result["artifacts"]["briefing_validation.json"], "created"
        )

if __name__ == "__main__":
    unittest.main()