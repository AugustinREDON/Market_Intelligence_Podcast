"""Store immutable run artifacts in private S3.

Importing this module does not contact AWS.
"""

import json
import os
import re

import boto3
from botocore.exceptions import ClientError


class S3RunStorage:
    def __init__(self, *, bucket=None, client=None):
        self.bucket = bucket or os.environ.get("MIP_S3_BUCKET")

        if not isinstance(self.bucket, str) or not self.bucket.strip():
            raise ValueError("MIP_S3_BUCKET must be configured")

        self.client = client if client is not None else boto3.client("s3")

    @staticmethod
    def _key(run_id, filename):
        if not isinstance(run_id, str) or not re.fullmatch(
            r"[0-9]{4}-[0-9]{2}-[0-9]{2}-[0-9]{3}", run_id
        ):
            raise ValueError("run_id must use YYYY-MM-DD-NNN")

        if not isinstance(filename, str) or not re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9_.-]*", filename
        ):
            raise ValueError("filename must be a simple filename")

        return f"runs/{run_id}/{filename}"

    def read_text(self, run_id, filename):
        """Read an existing artifact; propagate missing/access errors."""
        key = self._key(run_id, filename)
        response = self.client.get_object(
            Bucket=self.bucket,
            Key=key,
        )
        with response["Body"] as stream:
            return stream.read().decode("utf-8")

    def read_json(self, run_id, filename):
        return json.loads(self.read_text(run_id, filename))

    def preserve_text(
        self,
        run_id,
        filename,
        content,
        *,
        content_type="text/plain; charset=utf-8",
    ):
        """Return created/unchanged; refuse different existing content."""
        if not isinstance(content, str):
            raise TypeError("content must be a string")

        key = self._key(run_id, filename)

        try:
            self.client.put_object(
                Bucket=self.bucket,
                Key=key,
                Body=content.encode("utf-8"),
                ContentType=content_type,
                IfNoneMatch="*",
            )
        except ClientError as error:
            status = error.response.get(
                "ResponseMetadata", {}
            ).get("HTTPStatusCode")

            if status != 412:
                raise

            existing = self.read_text(run_id, filename)
            if existing != content:
                raise ValueError(
                    f"Existing S3 artifact differs; refusing to overwrite: "
                    f"{key}"
                ) from error

            return "unchanged"

        return "created"

    def preserve_json(self, run_id, filename, value):
        content = json.dumps(
            value,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
            allow_nan=False,
        )
        return self.preserve_text(
            run_id,
            filename,
            content,
            content_type="application/json",
        )