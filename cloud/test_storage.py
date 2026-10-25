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


if __name__ == "__main__":
    unittest.main()