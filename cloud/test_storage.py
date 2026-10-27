"""Offline tests for S3 artifact preservation."""

import io
import unittest

from botocore.exceptions import ClientError

from cloud.storage import S3RunStorage


class FakeS3:
    """Small in-memory substitute for the S3 operations we use."""

    def __init__(self):
        self.objects = {}

    def put_object(self, *, Bucket, Key, Body, ContentType, IfNoneMatch):
        if IfNoneMatch != "*":
            raise AssertionError("Writes must prevent overwriting")

        identity = (Bucket, Key)

        if identity in self.objects:
            raise ClientError(
                {
                    "Error": {"Code": "PreconditionFailed"},
                    "ResponseMetadata": {"HTTPStatusCode": 412},
                },
                "PutObject",
            )

        self.objects[identity] = Body
        return {}

    def get_object(self, *, Bucket, Key):
        return {"Body": io.BytesIO(self.objects[(Bucket, Key)])}


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.storage = S3RunStorage(
            bucket="offline-test",
            client=FakeS3(),
        )
        self.run_id = "2026-10-25-999"

    def test_create_and_read_json(self):
        value = {"message": "Briefing saved", "topics": []}

        result = self.storage.preserve_json(
            self.run_id, "inputs.json", value
        )

        self.assertEqual(result, "created")
        self.assertEqual(
            self.storage.read_json(self.run_id, "inputs.json"),
            value,
        )

    def test_identical_content_is_preserved(self):
        for expected in ("created", "unchanged"):
            result = self.storage.preserve_text(
                self.run_id, "briefing.md", "Original briefing"
            )
            self.assertEqual(result, expected)

    def test_different_content_is_rejected(self):
        self.storage.preserve_text(
            self.run_id, "briefing.md", "Original briefing"
        )

        with self.assertRaisesRegex(ValueError, "refusing to overwrite"):
            self.storage.preserve_text(
                self.run_id, "briefing.md", "Different briefing"
            )

        self.assertEqual(
            self.storage.read_text(self.run_id, "briefing.md"),
            "Original briefing",
        )

    def test_submission_can_only_be_claimed_once(self):
        first = self.storage.claim_briefing_submission(self.run_id)
        second = self.storage.claim_briefing_submission(self.run_id)

        self.assertTrue(first)
        self.assertFalse(second)

        claim = self.storage.read_json(
            self.run_id,
            "briefing_submission_claim.json",
        )
        self.assertEqual(claim["run_id"], self.run_id)
        self.assertEqual(claim["status"], "submission_claimed")

    def test_storage_error_does_not_authorize_submission(self):
        from unittest.mock import Mock

        client = Mock()
        client.put_object.side_effect = ClientError(
            {
                "Error": {"Code": "AccessDenied"},
                "ResponseMetadata": {"HTTPStatusCode": 403},
            },
            "PutObject",
        )
        storage = S3RunStorage(
            bucket="offline-test",
            client=client,
        )

        with self.assertRaises(ClientError):
            storage.claim_briefing_submission(self.run_id)

    def test_script_submission_can_only_be_claimed_once(self):
        first = self.storage.claim_script_submission(self.run_id)
        second = self.storage.claim_script_submission(self.run_id)

        self.assertTrue(first)
        self.assertFalse(second)
        self.assertEqual(
            self.storage.read_json(
                self.run_id,
                "script_submission_claim.json",
            ),
            {
                "run_id": self.run_id,
                "stage": "script",
                "status": "submission_claimed",
            },
        )

    def test_briefing_and_script_claims_are_independent(self):
        # Check both possible submission orders.
        for stages in (
            ("briefing", "script"),
            ("script", "briefing"),
        ):
            with self.subTest(order=stages):
                storage = S3RunStorage(
                    bucket="offline-test",
                    client=FakeS3(),
                )
                claims = {
                    "briefing": storage.claim_briefing_submission,
                    "script": storage.claim_script_submission,
                }

                for stage in stages:
                    self.assertTrue(claims[stage](self.run_id))

                for stage in stages:
                    self.assertFalse(claims[stage](self.run_id))
                    claim = storage.read_json(
                        self.run_id,
                        f"{stage}_submission_claim.json",
                    )
                    self.assertEqual(claim["stage"], stage)

    def test_script_storage_error_does_not_authorize_submission(self):
        from unittest.mock import Mock

        client = Mock()
        client.put_object.side_effect = ClientError(
            {
                "Error": {"Code": "AccessDenied"},
                "ResponseMetadata": {"HTTPStatusCode": 403},
            },
            "PutObject",
        )
        storage = S3RunStorage(
            bucket="offline-test",
            client=client,
        )

        with self.assertRaises(ClientError):
            storage.claim_script_submission(self.run_id)

if __name__ == "__main__":
    unittest.main()