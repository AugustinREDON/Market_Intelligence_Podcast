"""Prepare per-run audio configuration without starting the worker."""

import json
import os
from uuid import uuid4

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

from cloud.acceptance import json_sha256, load_accepted_script
from cloud.storage import S3RunStorage


AUDIO_REGION = "us-east-2"
AUDIO_INSTANCE_ID = "i-0acf87a780fb97103"
WORKER_LOCK_KEY = (
    f"runs/audio-worker/{AUDIO_INSTANCE_ID}/ownership.json"
)
EXECUTION_ARN_PREFIX = (
    "arn:aws:states:us-east-2:340015815972:"
    "execution:mip-text-pipeline:"
)

VOICE_REFERENCES = {
    "HOST_A": {
        "key": "assets/voices/v1/host_a.wav",
        "sha256": (
            "2de5ee15c37365fa550bab3d4ddd8dc496155"
            "d2316401d387be2795d5a841313"
        ),
    },
    "HOST_B": {
        "key": "assets/voices/v1/host_b.wav",
        "sha256": (
            "7147b74992842d22701c5c69a42b72b07a8"
            "033e6a97e6e4f9a1fa86619486768"
        ),
    },
}


def prepare_audio_run(*, run_id, storage=None):
    """Verify acceptance and preserve an immutable job for this run."""
    if storage is None:
        storage = S3RunStorage()

    # Verify the exact script and its accepted briefing dependency.
    accepted = load_accepted_script(
        run_id=run_id,
        storage=storage,
    )

    job = {
        "run_id": run_id,
        "bucket": storage.bucket,
        "region": AUDIO_REGION,
        "voices": {
            speaker: dict(reference)
            for speaker, reference in VOICE_REFERENCES.items()
        },
    }

    preservation = storage.preserve_json(
        run_id,
        "audio_job.json",
        job,
    )


    return {
        "status": "audio_job_prepared",
        "run_id": run_id,
        "bucket": storage.bucket,
        "region": AUDIO_REGION,
        "job_key": f"runs/{run_id}/audio_job.json",
        "job_sha256": json_sha256(job),
        "script_sha256": accepted["acceptance"]["script_sha256"],
        "preservation": preservation,
        "generation_requested": False,
    }

def acquire_audio_worker(
    *,
    run_id,
    execution_arn,
    bucket=None,
    client=None,
):
    """Acquire exclusive ownership once; never take over an existing lock."""
    S3RunStorage._key(run_id, "audio_job.json")

    if (
        not isinstance(execution_arn, str)
        or not execution_arn.startswith(EXECUTION_ARN_PREFIX)
        or not execution_arn[len(EXECUTION_ARN_PREFIX):].strip()
    ):
        raise ValueError("Expected an execution ARN for mip-text-pipeline")

    bucket = bucket or os.environ.get("MIP_S3_BUCKET")
    if not isinstance(bucket, str) or not bucket.strip():
        raise ValueError("MIP_S3_BUCKET must be configured")

    if client is None:
        client = boto3.client(
            "s3",
            region_name=AUDIO_REGION,
            config=Config(
                retries={"total_max_attempts": 1},
                connect_timeout=10,
                read_timeout=30,
            ),
        )

    owner_token = uuid4().hex
    ownership = {
        "schema_version": 1,
        "status": "ownership_held",
        "run_id": run_id,
        "execution_arn": execution_arn,
        "instance_id": AUDIO_INSTANCE_ID,
        "owner_token": owner_token,
    }
    result = {
        "run_id": run_id,
        "execution_arn": execution_arn,
        "instance_id": AUDIO_INSTANCE_ID,
        "bucket": bucket,
        "lock_key": WORKER_LOCK_KEY,
        "generation_requested": False,
    }

    try:
        response = client.put_object(
            Bucket=bucket,
            Key=WORKER_LOCK_KEY,
            Body=json.dumps(
                ownership,
                sort_keys=True,
                allow_nan=False,
            ).encode("utf-8"),
            ContentType="application/json",
            IfNoneMatch="*",
        )
    except ClientError as error:
        status = error.response.get(
            "ResponseMetadata", {}
        ).get("HTTPStatusCode")

        if status == 412:
            return {
                **result,
                "status": "audio_worker_busy",
            }

        # Conflicts, access failures, and uncertain writes do not
        # authorize starting or stopping the instance.
        raise

    return {
        **result,
        "status": "audio_worker_acquired",
        "owner_token": owner_token,
        "lock_etag": response["ETag"],
    }