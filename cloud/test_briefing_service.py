"""Offline tests for the cloud briefing submission flow."""

import unittest
from unittest.mock import MagicMock, Mock, patch

from cloud.briefing_service import submit_prepared_briefing_run
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


if __name__ == "__main__":
    unittest.main()