"""Offline checks for per-run audio job preparation."""

import unittest
from unittest.mock import Mock, patch

from cloud.acceptance import json_sha256
from cloud.audio_service import prepare_audio_run


class AudioServiceTests(unittest.TestCase):

    def test_lambda_routes_audio_preparation(self):
        from cloud.lambda_function import lambda_handler

        expected = {
            "status": "audio_job_prepared",
            "run_id": self.run_id,
            "generation_requested": False,
        }

        with patch(
            "cloud.audio_service.prepare_audio_run",
            return_value=expected,
        ) as prepare:
            result = lambda_handler(
                {
                    "action": "prepare_audio",
                    "run_id": self.run_id,
                },
                None,
            )

        prepare.assert_called_once_with(run_id=self.run_id)
        self.assertEqual(result, expected)

    def setUp(self):
        self.run_id = "2026-11-06-998"
        self.storage = Mock()
        self.storage.bucket = "offline-test"
        self.storage.preserve_json.return_value = "created"

        loader = patch("cloud.audio_service.load_accepted_script")
        self.loader = loader.start()
        self.addCleanup(loader.stop)
        self.loader.return_value = {
            "acceptance": {"script_sha256": "a" * 64},
        }

    def test_preparation_binds_job_to_verified_run(self):
        result = prepare_audio_run(
            run_id=self.run_id,
            storage=self.storage,
        )

        self.loader.assert_called_once_with(
            run_id=self.run_id,
            storage=self.storage,
        )
        self.storage.preserve_json.assert_called_once()
        saved_run, filename, job = (
            self.storage.preserve_json.call_args.args
        )

        self.assertEqual(saved_run, self.run_id)
        self.assertEqual(filename, "audio_job.json")
        self.assertEqual(job["run_id"], self.run_id)
        self.assertEqual(job["bucket"], "offline-test")
        self.assertEqual(job["region"], "us-east-2")
        self.assertEqual(set(job["voices"]), {"HOST_A", "HOST_B"})

        for speaker, reference in job["voices"].items():
            self.assertEqual(
                reference["key"],
                f"assets/voices/v1/{speaker.lower()}.wav",
            )
            self.assertRegex(reference["sha256"], r"^[0-9a-f]{64}$")

        self.assertEqual(result["status"], "audio_job_prepared")
        self.assertEqual(result["run_id"], self.run_id)
        self.assertEqual(
            result["job_key"],
            f"runs/{self.run_id}/audio_job.json",
        )
        self.assertEqual(result["job_sha256"], json_sha256(job))
        self.assertEqual(result["script_sha256"], "a" * 64)
        self.assertFalse(result["generation_requested"])

    def test_acceptance_failure_prevents_job_write(self):
        self.loader.side_effect = ValueError("Acceptance mismatch")

        with self.assertRaisesRegex(ValueError, "Acceptance mismatch"):
            prepare_audio_run(
                run_id=self.run_id,
                storage=self.storage,
            )

        self.storage.preserve_json.assert_not_called()

    def test_storage_conflict_is_not_retried_or_hidden(self):
        self.storage.preserve_json.side_effect = ValueError(
            "Existing S3 artifact differs"
        )

        with self.assertRaisesRegex(ValueError, "artifact differs"):
            prepare_audio_run(
                run_id=self.run_id,
                storage=self.storage,
            )

        self.storage.preserve_json.assert_called_once()


if __name__ == "__main__":
    unittest.main()