"""Offline checks for per-run audio job preparation."""
import json

from botocore.exceptions import ClientError

import unittest
from unittest.mock import Mock, patch

from cloud.acceptance import json_sha256
from cloud.audio_service import (
    AUDIO_INSTANCE_ID,
    EXECUTION_ARN_PREFIX,
    WORKER_LOCK_KEY,
    acquire_audio_worker,
    prepare_audio_run,
)


class AudioServiceTests(unittest.TestCase):

    def test_worker_lock_blocks_competing_and_repeated_acquisition(self):
        client = Mock()
        saved = {}

        def create_once(**kwargs):
            self.assertEqual(kwargs["IfNoneMatch"], "*")
            self.assertEqual(kwargs["Bucket"], "offline-test")
            self.assertEqual(kwargs["Key"], WORKER_LOCK_KEY)

            if saved:
                raise ClientError(
                    {
                        "Error": {"Code": "PreconditionFailed"},
                        "ResponseMetadata": {"HTTPStatusCode": 412},
                    },
                    "PutObject",
                )

            saved["ownership"] = json.loads(kwargs["Body"])
            return {"ETag": '"offline-lock-etag"'}

        client.put_object.side_effect = create_once
        execution_arn = EXECUTION_ARN_PREFIX + "offline-first"

        first = acquire_audio_worker(
            run_id=self.run_id,
            execution_arn=execution_arn,
            bucket="offline-test",
            client=client,
        )

        self.assertEqual(first["status"], "audio_worker_acquired")
        self.assertEqual(first["instance_id"], AUDIO_INSTANCE_ID)
        self.assertEqual(first["lock_etag"], '"offline-lock-etag"')
        self.assertFalse(first["generation_requested"])

        ownership = saved["ownership"].copy()
        self.assertEqual(ownership["run_id"], self.run_id)
        self.assertEqual(ownership["execution_arn"], execution_arn)
        self.assertEqual(
            ownership["owner_token"],
            first["owner_token"],
        )

        for run_id, owner in (
            ("2026-11-06-999", EXECUTION_ARN_PREFIX + "offline-second"),
            (self.run_id, execution_arn),
        ):
            with self.subTest(owner=owner):
                blocked = acquire_audio_worker(
                    run_id=run_id,
                    execution_arn=owner,
                    bucket="offline-test",
                    client=client,
                )
                self.assertEqual(blocked["status"], "audio_worker_busy")
                self.assertNotIn("owner_token", blocked)
                self.assertNotIn("lock_etag", blocked)
                self.assertEqual(saved["ownership"], ownership)

        self.assertEqual(client.put_object.call_count, 3)
        client.delete_object.assert_not_called()

    def test_worker_lock_errors_do_not_authorize_or_retry(self):
        errors = [
            TimeoutError("Uncertain write response"),
            ClientError(
                {
                    "Error": {"Code": "ConditionalRequestConflict"},
                    "ResponseMetadata": {"HTTPStatusCode": 409},
                },
                "PutObject",
            ),
            ClientError(
                {
                    "Error": {"Code": "AccessDenied"},
                    "ResponseMetadata": {"HTTPStatusCode": 403},
                },
                "PutObject",
            ),
        ]

        for error in errors:
            with self.subTest(error=str(error)):
                client = Mock()
                client.put_object.side_effect = error

                with self.assertRaises(type(error)) as caught:
                    acquire_audio_worker(
                        run_id=self.run_id,
                        execution_arn=(
                            EXECUTION_ARN_PREFIX + "offline-uncertain"
                        ),
                        bucket="offline-test",
                        client=client,
                    )

                self.assertIs(caught.exception, error)
                client.put_object.assert_called_once()
                client.delete_object.assert_not_called()

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