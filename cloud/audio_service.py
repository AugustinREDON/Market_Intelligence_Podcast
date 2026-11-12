"""Prepare per-run audio configuration without starting the worker."""

import json
import os
from uuid import uuid4
import shlex
import boto3
from botocore.config import Config
from botocore.exceptions import ClientError
from datetime import datetime, timezone
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

def _read_audio_worker_state(ec2_client):
    """Read the fixed worker instance and reject unexpected identities/states."""
    description = ec2_client.describe_instances(
        InstanceIds=[AUDIO_INSTANCE_ID],
    )
    instances = [
        instance
        for reservation in description.get("Reservations", [])
        for instance in reservation.get("Instances", [])
    ]
    if (
        len(instances) != 1
        or instances[0].get("InstanceId") != AUDIO_INSTANCE_ID
    ):
        raise ValueError("Could not verify the expected audio instance")

    state = instances[0].get("State", {}).get("Name")
    if state not in {"pending", "running", "stopping", "stopped"}:
        raise ValueError(f"Unexpected audio instance state: {state}")
    return state


def check_audio_worker_shutdown(
    *, run_id, execution_arn, owner_token, lock_etag,
    bucket=None, client=None, ec2_client=None,
):
    """Observe shutdown under ownership; do not stop EC2 or release the lock."""
    bucket = bucket or os.environ.get("MIP_S3_BUCKET")
    if not isinstance(bucket, str) or not bucket.strip():
        raise ValueError("MIP_S3_BUCKET must be configured")

    config = Config(
        retries={"total_max_attempts": 1},
        connect_timeout=10,
        read_timeout=30,
    )
    if client is None:
        client = boto3.client(
            "s3", region_name=AUDIO_REGION, config=config,
        )

    _verify_worker_lock(
        run_id=run_id,
        execution_arn=execution_arn,
        owner_token=owner_token,
        lock_etag=lock_etag,
        bucket=bucket,
        client=client,
    )
    if ec2_client is None:
        ec2_client = boto3.client(
            "ec2", region_name=AUDIO_REGION, config=config,
        )

    state = _read_audio_worker_state(ec2_client)
    return {
        "status": (
            "audio_worker_shutdown_confirmed"
            if state == "stopped"
            else "audio_worker_shutdown_pending"
        ),
        "run_id": run_id,
        "execution_arn": execution_arn,
        "instance_id": AUDIO_INSTANCE_ID,
        "instance_state": state,
        "observed_at": datetime.now(timezone.utc).isoformat(),
        "generation_requested": False,
    }

def release_audio_worker(
    *,
    run_id,
    execution_arn,
    owner_token,
    lock_etag,
    bucket=None,
    client=None,
    ec2_client=None,
):
    """Release verified ownership after the worker is confirmed stopped.

    The caller must reconcile any outstanding start requests first.
    This function never starts or stops EC2.
    """
    S3RunStorage._key(run_id, "audio_job.json")

    if (
        not isinstance(execution_arn, str)
        or not execution_arn.startswith(EXECUTION_ARN_PREFIX)
        or not execution_arn[len(EXECUTION_ARN_PREFIX):].strip()
    ):
        raise ValueError("Expected an execution ARN for mip-text-pipeline")

    if not isinstance(owner_token, str) or not owner_token.strip():
        raise ValueError("Expected an ownership token")

    if (
        not isinstance(lock_etag, str)
        or not lock_etag.strip()
        or lock_etag.strip() == "*"
    ):
        raise ValueError("Expected a specific lock ETag")

    bucket = bucket or os.environ.get("MIP_S3_BUCKET")
    if not isinstance(bucket, str) or not bucket.strip():
        raise ValueError("MIP_S3_BUCKET must be configured")

    config = Config(
        retries={"total_max_attempts": 1},
        connect_timeout=10,
        read_timeout=30,
    )

    if client is None:
        client = boto3.client(
            "s3",
            region_name=AUDIO_REGION,
            config=config,
        )

    response = client.get_object(
        Bucket=bucket,
        Key=WORKER_LOCK_KEY,
        IfMatch=lock_etag,
    )
    with response["Body"] as stream:
        ownership = json.load(stream)

    expected = {
        "schema_version": 1,
        "status": "ownership_held",
        "run_id": run_id,
        "execution_arn": execution_arn,
        "instance_id": AUDIO_INSTANCE_ID,
        "owner_token": owner_token,
    }

    if (
        ownership != expected
        or response.get("ETag") != lock_etag
    ):
        raise ValueError("Worker ownership does not match this execution")

    # Do not inspect or modify EC2 without verified ownership.
    if ec2_client is None:
        ec2_client = boto3.client(
            "ec2",
            region_name=AUDIO_REGION,
            config=config,
        )

    description = ec2_client.describe_instances(
        InstanceIds=[AUDIO_INSTANCE_ID],
    )
    instances = [
        instance
        for reservation in description.get("Reservations", [])
        for instance in reservation.get("Instances", [])
    ]

    if (
        len(instances) != 1
        or instances[0].get("InstanceId") != AUDIO_INSTANCE_ID
    ):
        raise ValueError("Could not verify the expected audio instance")

    state = instances[0].get("State", {}).get("Name")
    if state not in {
        "pending",
        "running",
        "stopping",
        "stopped",
    }:
        raise ValueError(f"Unexpected audio instance state: {state}")

    result = {
        "run_id": run_id,
        "execution_arn": execution_arn,
        "instance_id": AUDIO_INSTANCE_ID,
        "instance_state": state,
        "generation_requested": False,
    }

    if state != "stopped":
        return {
            **result,
            "status": "audio_worker_not_stopped",
        }

    # A changed lock or uncertain delete must propagate as an error.
    deleted = client.delete_object(
        Bucket=bucket,
        Key=WORKER_LOCK_KEY,
        IfMatch=lock_etag,
    )
    if deleted.get("ResponseMetadata", {}).get("HTTPStatusCode") != 204:
        raise RuntimeError("Worker lock release was not confirmed")

    return {
        **result,
        "status": "audio_worker_released",
    }

def _verify_worker_lock(
    *,
    run_id,
    execution_arn,
    owner_token,
    lock_etag,
    bucket,
    client,
):
    """Verify the current ownership record before an EC2 operation."""
    S3RunStorage._key(run_id, "audio_job.json")

    if (
        not isinstance(lock_etag, str)
        or not lock_etag.strip()
        or lock_etag.strip() == "*"
    ):
        raise ValueError("Expected a specific lock ETag")

    response = client.get_object(
        Bucket=bucket,
        Key=WORKER_LOCK_KEY,
        IfMatch=lock_etag,
    )
    with response["Body"] as stream:
        ownership = json.load(stream)

    expected = {
        "schema_version": 1,
        "status": "ownership_held",
        "run_id": run_id,
        "execution_arn": execution_arn,
        "instance_id": AUDIO_INSTANCE_ID,
        "owner_token": owner_token,
    }
    if ownership != expected or response.get("ETag") != lock_etag:
        raise ValueError("Worker ownership does not match this execution")

def _load_audio_start_receipt(storage, run_id, execution_arn, owner_token):
    """Require matching saved evidence of an acknowledged EC2 start."""
    claim_name = "audio_instance_start_claim.json"
    claim = storage.read_json(run_id, claim_name)
    receipt = storage.read_json(run_id, "audio_instance_start_receipt.json")
    if not isinstance(claim, dict) or not isinstance(receipt, dict):
        raise ValueError("Expected saved EC2 startup claim and receipt")

    expected_claim = {
        "run_id": run_id,
        "execution_arn": execution_arn,
        "instance_id": AUDIO_INSTANCE_ID,
        "owner_token": owner_token,
        "requested_at": claim.get("requested_at"),
        "automatic_retry_allowed": False,
    }
    if (
        claim != expected_claim
        or claim.get("automatic_retry_allowed") is not False
    ):
        raise ValueError("EC2 startup claim does not match current ownership")

    if not all(
        isinstance(value, str)
        for value in (
            claim.get("requested_at"),
            receipt.get("acknowledged_at"),
        )
    ):
        raise ValueError("Expected EC2 startup timestamps")

    requested = datetime.fromisoformat(claim["requested_at"])
    acknowledged = datetime.fromisoformat(receipt["acknowledged_at"])
    if (
        requested.utcoffset() is None
        or acknowledged.utcoffset() is None
        or not requested <= acknowledged <= datetime.now(timezone.utc)
    ):
        raise ValueError("Invalid EC2 startup receipt timing")

    expected_receipt = {
        **claim,
        "status": "audio_instance_start_acknowledged",
        "start_claim_key": S3RunStorage._key(run_id, claim_name),
        "acknowledged_at": receipt["acknowledged_at"],
        "previous_state": "stopped",
        "current_state": receipt.get("current_state"),
    }
    if (
        receipt != expected_receipt
        or receipt.get("automatic_retry_allowed") is not False
        or receipt.get("current_state") not in {"pending", "running"}
    ):
        raise ValueError("EC2 startup receipt does not match its claim")

    return receipt


def submit_audio_worker_stop(
    *, run_id, execution_arn, owner_token, lock_etag,
    bucket=None, client=None, ec2_client=None,
):
    """Request one normal EC2 stop; never release ownership or retry."""
    bucket = bucket or os.environ.get("MIP_S3_BUCKET")
    if not isinstance(bucket, str) or not bucket.strip():
        raise ValueError("MIP_S3_BUCKET must be configured")

    config = Config(
        retries={"total_max_attempts": 1},
        connect_timeout=10,
        read_timeout=30,
    )
    if client is None:
        client = boto3.client(
            "s3", region_name=AUDIO_REGION, config=config,
        )
    _verify_worker_lock(
        run_id=run_id,
        execution_arn=execution_arn,
        owner_token=owner_token,
        lock_etag=lock_etag,
        bucket=bucket,
        client=client,
    )
    storage = S3RunStorage(bucket=bucket, client=client)
    startup = _load_audio_start_receipt(
        storage, run_id, execution_arn, owner_token
    )
    if ec2_client is None:
        ec2_client = boto3.client(
            "ec2", region_name=AUDIO_REGION, config=config,
        )

    state = _read_audio_worker_state(ec2_client)
    base = {
        "run_id": run_id,
        "execution_arn": execution_arn,
        "instance_id": AUDIO_INSTANCE_ID,
        "generation_requested": False,
    }
    if state != "running":
        return {
            **base,
            "status": (
                "audio_worker_shutdown_confirmed"
                if state == "stopped"
                else "audio_worker_shutdown_pending"
            ),
            "instance_state": state,
            "stop_requested": False,
        }

    claim_name = "audio_instance_stop_claim.json"
    claim_key = S3RunStorage._key(run_id, claim_name)
    claim = {
        **base,
        "owner_token": owner_token,
        "lock_etag": lock_etag,
        "startup_receipt_sha256": json_sha256(startup),
        "requested_at": datetime.now(timezone.utc).isoformat(),
        "automatic_retry_allowed": False,
    }
    client.put_object(
        Bucket=bucket,
        Key=claim_key,
        Body=json.dumps(
            claim, sort_keys=True, allow_nan=False
        ).encode("utf-8"),
        ContentType="application/json",
        IfNoneMatch="*",
    )

    response = ec2_client.stop_instances(
        InstanceIds=[AUDIO_INSTANCE_ID],
    )
    transitions = (
        response.get("StoppingInstances")
        if isinstance(response, dict)
        else None
    )
    if (
        not isinstance(transitions, list)
        or len(transitions) != 1
        or not isinstance(transitions[0], dict)
    ):
        raise RuntimeError("EC2 stop outcome uncertain; reconcile before recovery")

    transition = transitions[0]
    previous = transition.get("PreviousState")
    current = transition.get("CurrentState")
    if (
        transition.get("InstanceId") != AUDIO_INSTANCE_ID
        or not isinstance(previous, dict)
        or previous.get("Name") not in {"running", "stopping", "stopped"}
        or not isinstance(current, dict)
        or current.get("Name") not in {"stopping", "stopped"}
    ):
        raise RuntimeError("EC2 stop outcome uncertain; reconcile before recovery")

    receipt_name = "audio_instance_stop_receipt.json"
    receipt_key = S3RunStorage._key(run_id, receipt_name)
    receipt = {
        **claim,
        "status": "audio_instance_stop_acknowledged",
        "stop_claim_key": claim_key,
        "acknowledged_at": datetime.now(timezone.utc).isoformat(),
        "previous_state": previous["Name"],
        "current_state": current["Name"],
    }
    client.put_object(
        Bucket=bucket,
        Key=receipt_key,
        Body=json.dumps(
            receipt, sort_keys=True, allow_nan=False
        ).encode("utf-8"),
        ContentType="application/json",
        IfNoneMatch="*",
    )
    return {
        **base,
        "status": "audio_worker_stop_requested",
        "stop_claim_key": claim_key,
        "stop_receipt_key": receipt_key,
        "stop_requested": True,
    }


def start_audio_worker(
    *,
    run_id,
    execution_arn,
    owner_token,
    lock_etag,
    bucket=None,
    client=None,
    ec2_client=None,
):
    """Request one EC2 start under verified ownership.

    An existing or uncertain start claim never authorizes another request.
    This function does not start the audio service.
    """
    bucket = bucket or os.environ.get("MIP_S3_BUCKET")
    if not isinstance(bucket, str) or not bucket.strip():
        raise ValueError("MIP_S3_BUCKET must be configured")

    config = Config(
        retries={"total_max_attempts": 1},
        connect_timeout=10,
        read_timeout=30,
    )
    if client is None:
        client = boto3.client(
            "s3",
            region_name=AUDIO_REGION,
            config=config,
        )

    _verify_worker_lock(
        run_id=run_id,
        execution_arn=execution_arn,
        owner_token=owner_token,
        lock_etag=lock_etag,
        bucket=bucket,
        client=client,
    )

    if ec2_client is None:
        ec2_client = boto3.client(
            "ec2",
            region_name=AUDIO_REGION,
            config=config,
        )

    description = ec2_client.describe_instances(
        InstanceIds=[AUDIO_INSTANCE_ID],
    )
    instances = [
        instance
        for reservation in description.get("Reservations", [])
        for instance in reservation.get("Instances", [])
    ]
    if (
        len(instances) != 1
        or instances[0].get("InstanceId") != AUDIO_INSTANCE_ID
    ):
        raise ValueError("Could not verify the expected audio instance")

    state = instances[0].get("State", {}).get("Name")
    if state != "stopped":
        raise ValueError(
            f"Audio instance must be stopped before startup; found {state}"
        )

    requested_at = datetime.now(timezone.utc).isoformat()
    claim_key = S3RunStorage._key(
        run_id,
        "audio_instance_start_claim.json",
    )
    claim = {
        "run_id": run_id,
        "execution_arn": execution_arn,
        "instance_id": AUDIO_INSTANCE_ID,
        "owner_token": owner_token,
        "requested_at": requested_at,
        "automatic_retry_allowed": False,
    }

    # Never retry this write or proceed after an uncertain response.
    client.put_object(
        Bucket=bucket,
        Key=claim_key,
        Body=json.dumps(
            claim,
            sort_keys=True,
            allow_nan=False,
        ).encode("utf-8"),
        ContentType="application/json",
        IfNoneMatch="*",
    )

    # Never automatically repeat an uncertain EC2 start request.
    response = ec2_client.start_instances(
        InstanceIds=[AUDIO_INSTANCE_ID],
    )
    started = (
        response.get("StartingInstances")
        if isinstance(response, dict)
        else None
    )
    if (
        not isinstance(started, list)
        or len(started) != 1
        or not isinstance(started[0], dict)
    ):
        raise RuntimeError(
            "EC2 start outcome uncertain; inspect before recovery"
        )

    transition = started[0]
    previous = transition.get("PreviousState")
    current = transition.get("CurrentState")
    if (
        transition.get("InstanceId") != AUDIO_INSTANCE_ID
        or not isinstance(previous, dict)
        or previous.get("Name") != "stopped"
        or not isinstance(current, dict)
        or current.get("Name") not in {"pending", "running"}
    ):
        raise RuntimeError(
            "EC2 start outcome uncertain; inspect before recovery"
        )

    receipt_key = S3RunStorage._key(
        run_id,
        "audio_instance_start_receipt.json",
    )
    receipt = {
        **claim,
        "status": "audio_instance_start_acknowledged",
        "start_claim_key": claim_key,
        "acknowledged_at": datetime.now(timezone.utc).isoformat(),
        "previous_state": previous["Name"],
        "current_state": current["Name"],
    }

    # A failed or uncertain receipt write must propagate.
    # The original claim remains and prevents another startup attempt.
    client.put_object(
        Bucket=bucket,
        Key=receipt_key,
        Body=json.dumps(
            receipt,
            sort_keys=True,
            allow_nan=False,
        ).encode("utf-8"),
        ContentType="application/json",
        IfNoneMatch="*",
    )

    return {
        "status": "audio_instance_start_requested",
        "run_id": run_id,
        "execution_arn": execution_arn,
        "instance_id": AUDIO_INSTANCE_ID,
        "requested_at": requested_at,
        "start_claim_key": claim_key,
        "start_receipt_key": receipt_key,
        "generation_requested": False,
    }


def check_audio_worker_connection(
    *,
    run_id,
    execution_arn,
    owner_token,
    lock_etag,
    bucket=None,
    client=None,
    ec2_client=None,
    ssm_client=None,
):
    """Check EC2 and a fresh SSM connection without changing the worker."""
    bucket = bucket or os.environ.get("MIP_S3_BUCKET")
    if not isinstance(bucket, str) or not bucket.strip():
        raise ValueError("MIP_S3_BUCKET must be configured")

    config = Config(
        retries={"total_max_attempts": 1},
        connect_timeout=10,
        read_timeout=30,
    )
    if client is None:
        client = boto3.client(
            "s3",
            region_name=AUDIO_REGION,
            config=config,
        )

    _verify_worker_lock(
        run_id=run_id,
        execution_arn=execution_arn,
        owner_token=owner_token,
        lock_etag=lock_etag,
        bucket=bucket,
        client=client,
    )

    claim_key = S3RunStorage._key(
        run_id,
        "audio_instance_start_claim.json",
    )
    response = client.get_object(
        Bucket=bucket,
        Key=claim_key,
    )
    with response["Body"] as stream:
        claim = json.load(stream)

    expected = {
        "run_id": run_id,
        "execution_arn": execution_arn,
        "instance_id": AUDIO_INSTANCE_ID,
        "owner_token": owner_token,
        "automatic_retry_allowed": False,
    }
    if (
        not isinstance(claim, dict)
        or any(claim.get(key) != value for key, value in expected.items())
        or claim.get("automatic_retry_allowed") is not False
    ):
        raise ValueError("Start claim does not match this execution")

    requested_at = datetime.fromisoformat(claim["requested_at"])
    if requested_at.tzinfo is None or requested_at.utcoffset() is None:
        raise ValueError("Start claim timestamp must include a timezone")
    if requested_at > datetime.now(timezone.utc):
        raise ValueError("Start claim timestamp is in the future")

    if ec2_client is None:
        ec2_client = boto3.client(
            "ec2",
            region_name=AUDIO_REGION,
            config=config,
        )

    description = ec2_client.describe_instances(
        InstanceIds=[AUDIO_INSTANCE_ID],
    )
    instances = [
        instance
        for reservation in description.get("Reservations", [])
        for instance in reservation.get("Instances", [])
    ]
    if (
        len(instances) != 1
        or instances[0].get("InstanceId") != AUDIO_INSTANCE_ID
    ):
        raise ValueError("Could not verify the expected audio instance")

    state = instances[0].get("State", {}).get("Name")
    result = {
        "run_id": run_id,
        "execution_arn": execution_arn,
        "instance_id": AUDIO_INSTANCE_ID,
        "instance_state": state,
        "requested_at": requested_at.isoformat(),
        "generation_requested": False,
    }

    if state == "pending":
        return {
            **result,
            "status": "audio_worker_connection_pending",
            "reason": "instance_pending",
        }
    if state != "running":
        raise ValueError(
            f"Audio instance is not starting or running; found {state}"
        )

    if ssm_client is None:
        ssm_client = boto3.client(
            "ssm",
            region_name=AUDIO_REGION,
            config=config,
        )

    information = ssm_client.describe_instance_information(
        Filters=[
            {
                "Key": "InstanceIds",
                "Values": [AUDIO_INSTANCE_ID],
            }
        ],
    )
    nodes = information.get("InstanceInformationList", [])
    if not nodes:
        return {
            **result,
            "status": "audio_worker_connection_pending",
            "reason": "ssm_not_registered",
        }
    if (
        len(nodes) != 1
        or nodes[0].get("InstanceId") != AUDIO_INSTANCE_ID
    ):
        raise ValueError("Could not verify the expected SSM node")

    node = nodes[0]
    ping = node.get("LastPingDateTime")
    fresh_ping = (
        isinstance(ping, datetime)
        and ping.tzinfo is not None
        and ping.utcoffset() is not None
        and requested_at < ping <= datetime.now(timezone.utc)
    )
    if node.get("PingStatus") != "Online" or not fresh_ping:
        return {
            **result,
            "status": "audio_worker_connection_pending",
            "reason": "ssm_not_fresh_online",
        }

    return {
        **result,
        "status": "audio_worker_connection_ready",
        "ssm_last_ping_at": ping.isoformat(),
    }

AUDIO_SAFEGUARD_PROBE = r'''
import json
import subprocess
import sys
from pathlib import Path


def systemctl(*arguments):
    return subprocess.run(
        ["/usr/bin/systemctl", *arguments],
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    ).stdout.strip()


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def normalized_unit(text):
    return [
        line.strip()
        for line in text.splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]


expected_units = {
    "mip-max-runtime.service": """
[Unit]
Description=Stop MIP worker at maximum runtime

[Service]
Type=oneshot
ExecStart=/usr/bin/systemctl poweroff
""",
    "mip-max-runtime.timer": """
[Unit]
Description=MIP worker maximum runtime from each boot

[Timer]
OnBootSec=90min
AccuracySec=1s
Unit=mip-max-runtime.service

[Install]
WantedBy=timers.target
""",
    "mip-audio-job.service": """
[Unit]
Description=Generate one MIP episode and stop the worker
Wants=network-online.target
After=network-online.target

[Service]
Type=exec
User=mip-audio
Group=mip-audio
WorkingDirectory=/opt/mip/app
EnvironmentFile=/etc/mip/audio.env
NoNewPrivileges=true
UMask=0077
ExecStartPre=+/usr/bin/systemctl is-active --quiet mip-max-runtime.timer
ExecStart=/opt/mip/venv/bin/python -m cloud.audio_job --job-file /opt/mip/app/audio_job.json --work-root /opt/mip/work
ExecStopPost=+/usr/bin/systemctl --no-block poweroff
Restart=no
TimeoutStopSec=300
StandardOutput=append:/var/log/mip/audio_worker.log
StandardError=inherit
""",
}

for unit, expected in expected_units.items():
    require(
        systemctl("show", unit, "--property=LoadState", "--value")
        == "loaded",
        f"Unit is not loaded: {unit}",
    )
    require(
        systemctl(
            "show",
            unit,
            "--property=NeedDaemonReload",
            "--value",
        ) == "no",
        f"Unit requires daemon reload: {unit}",
    )
    require(
        normalized_unit(systemctl("cat", unit))
        == normalized_unit(expected),
        f"Unexpected unit configuration: {unit}",
    )

require(
    systemctl("is-enabled", "mip-max-runtime.timer") == "enabled",
    "Shutdown timer is not enabled",
)
require(
    systemctl(
        "show",
        "mip-max-runtime.timer",
        "--property=ActiveState",
        "--value",
    ) == "active",
    "Shutdown timer is not active",
)
require(
    systemctl(
        "show",
        "mip-max-runtime.timer",
        "--property=SubState",
        "--value",
    ) == "waiting",
    "Shutdown timer is not waiting",
)

deadline_us = int(systemctl(
    "show",
    "mip-max-runtime.timer",
    "--property=NextElapseUSecMonotonic",
    "--value",
))
uptime_seconds = float(
    Path("/proc/uptime").read_text().split()[0]
)
require(
    uptime_seconds * 1_000_000 < deadline_us <= 5_401_000_000,
    "Shutdown deadline is expired or exceeds the expected boot limit",
)

require(
    systemctl(
        "show",
        "mip-audio-job.service",
        "--property=UnitFileState",
        "--value",
    ) == "static",
    "Audio service must remain static and not enabled on boot",
)
require(
    systemctl(
        "show",
        "mip-audio-job.service",
        "--property=ActiveState",
        "--value",
    ) == "inactive",
    "Audio service is not inactive",
)
require(
    systemctl(
        "show",
        "mip-max-runtime.service",
        "--property=ActiveState",
        "--value",
    ) == "inactive",
    "Shutdown service is already active",
)

print(json.dumps({
    "status": "audio_safeguards_verified",
    "probe_token": sys.argv[1],
    "boot_id": (
        Path("/proc/sys/kernel/random/boot_id")
        .read_text()
        .strip()
    ),
    "uptime_seconds": uptime_seconds,
    "shutdown_deadline_monotonic_us": deadline_us,
    "generation_requested": False,
}, sort_keys=True))
'''


def build_audio_safeguard_command(*, probe_token):
    """Build a read-only guest check; do not submit it to SSM."""
    import shlex

    if (
        not isinstance(probe_token, str)
        or len(probe_token) != 32
        or any(
            character not in "0123456789abcdef"
            for character in probe_token
        )
    ):
        raise ValueError(
            "Expected a 32-character lowercase hex probe token"
        )

    compile(
        AUDIO_SAFEGUARD_PROBE,
        "<audio-safeguard-probe>",
        "exec",
    )
    return (
        "/opt/mip/venv/bin/python -c "
        + shlex.quote(AUDIO_SAFEGUARD_PROBE)
        + " "
        + shlex.quote(probe_token)
    )

def validate_audio_safeguard_output(*, output, probe_token):
    """Validate probe output; this alone does not authorize audio startup."""
    from uuid import UUID

    if (
        not isinstance(probe_token, str)
        or len(probe_token) != 32
        or any(c not in "0123456789abcdef" for c in probe_token)
    ):
        raise ValueError("Invalid expected safeguard probe token")

    if not isinstance(output, str) or not 0 < len(output) <= 4096:
        raise ValueError("Missing or oversized safeguard output")

    def unique_fields(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("Duplicate safeguard output field")
            result[key] = value
        return result

    def reject_constant(value):
        raise ValueError(f"Invalid JSON constant: {value}")

    result = json.loads(
        output,
        object_pairs_hook=unique_fields,
        parse_constant=reject_constant,
    )
    expected_fields = {
        "status",
        "probe_token",
        "boot_id",
        "uptime_seconds",
        "shutdown_deadline_monotonic_us",
        "generation_requested",
    }
    if not isinstance(result, dict) or set(result) != expected_fields:
        raise ValueError("Unexpected safeguard output fields")

    if (
        result["status"] != "audio_safeguards_verified"
        or result["probe_token"] != probe_token
        or result["generation_requested"] is not False
    ):
        raise ValueError("Safeguard output does not match this probe")

    boot_id = result["boot_id"]
    if not isinstance(boot_id, str) or str(UUID(boot_id)) != boot_id:
        raise ValueError("Expected a canonical guest boot UUID")

    uptime = result["uptime_seconds"]
    deadline = result["shutdown_deadline_monotonic_us"]
    if (
        type(uptime) not in (int, float)
        or not 0 <= uptime < 5401
        or type(deadline) is not int
        or not uptime * 1_000_000 < deadline <= 5_401_000_000
    ):
        raise ValueError("Invalid safeguard uptime or shutdown deadline")

    return result

def build_audio_safeguard_request(*, run_id, bucket, probe_token):
    """Build the exact SSM request shared by submission and retrieval."""
    S3RunStorage._key(run_id, "audio_safeguard_submission_claim.json")
    return {
        "InstanceIds": [AUDIO_INSTANCE_ID],
        "DocumentName": "AWS-RunShellScript",
        "TimeoutSeconds": 60,
        "Comment": f"MIP safeguards {probe_token}",
        "Parameters": {
            "commands": [
                build_audio_safeguard_command(probe_token=probe_token)
            ],
            "executionTimeout": ["300"],
            "workingDirectory": ["/opt/mip/app"],
        },
        "OutputS3Region": AUDIO_REGION,
        "OutputS3BucketName": bucket,
        "OutputS3KeyPrefix": (
            f"runs/{run_id}/audio/safeguards/{probe_token}"
        ),
    }



def submit_audio_safeguard_check(
    *,
    run_id,
    execution_arn,
    owner_token,
    lock_etag,
    bucket=None,
    client=None,
    ec2_client=None,
    ssm_client=None,
):
    """Submit one read-only safeguard check under verified ownership."""
    from uuid import UUID

    bucket = bucket or os.environ.get("MIP_S3_BUCKET")
    if not isinstance(bucket, str) or not bucket.strip():
        raise ValueError("MIP_S3_BUCKET must be configured")

    config = Config(
        retries={"total_max_attempts": 1},
        connect_timeout=10,
        read_timeout=30,
    )
    if client is None:
        client = boto3.client(
            "s3",
            region_name=AUDIO_REGION,
            config=config,
        )
    if ssm_client is None:
        ssm_client = boto3.client(
            "ssm",
            region_name=AUDIO_REGION,
            config=config,
        )

    connection = check_audio_worker_connection(
        run_id=run_id,
        execution_arn=execution_arn,
        owner_token=owner_token,
        lock_etag=lock_etag,
        bucket=bucket,
        client=client,
        ec2_client=ec2_client,
        ssm_client=ssm_client,
    )
    if connection["status"] == "audio_worker_connection_pending":
        return connection
    if connection["status"] != "audio_worker_connection_ready":
        raise ValueError("Unexpected audio worker connection result")

    probe_token = uuid4().hex
    requested_at = datetime.now(timezone.utc).isoformat()
    request = build_audio_safeguard_request(
        run_id=run_id,
        bucket=bucket,
        probe_token=probe_token,
    )
    output_prefix = request["OutputS3KeyPrefix"]


    claim_key = S3RunStorage._key(
        run_id,
        "audio_safeguard_submission_claim.json",
    )
    claim = {
        "run_id": run_id,
        "execution_arn": execution_arn,
        "instance_id": AUDIO_INSTANCE_ID,
        "owner_token": owner_token,
        "probe_token": probe_token,
        "requested_at": requested_at,
        "request_sha256": json_sha256(request),
        "output_prefix": output_prefix,
        "automatic_retry_allowed": False,
        "generation_requested": False,
    }

    # An existing or uncertain claim blocks any further submission.
    client.put_object(
        Bucket=bucket,
        Key=claim_key,
        Body=json.dumps(
            claim,
            sort_keys=True,
            allow_nan=False,
        ).encode("utf-8"),
        ContentType="application/json",
        IfNoneMatch="*",
    )

    # Never automatically repeat a submission after an uncertain response.
    response = ssm_client.send_command(**request)
    submitted = response.get("Command", {})
    command_id = submitted.get("CommandId")

    try:
        valid_command_id = (
            isinstance(command_id, str)
            and str(UUID(command_id)) == command_id
        )
    except (ValueError, AttributeError):
        valid_command_id = False

    if (
        not valid_command_id
        or submitted.get("InstanceIds") != [AUDIO_INSTANCE_ID]
        or submitted.get("DocumentName") != "AWS-RunShellScript"
        or submitted.get("Comment") != request["Comment"]
    ):
        raise RuntimeError(
            "SSM safeguard submission outcome uncertain; "
            "inspect the saved claim and SSM commands before recovery"
        )

    receipt_key = S3RunStorage._key(
        run_id,
        "audio_safeguard_submission.json",
    )
    receipt = {
        **claim,
        "status": "audio_safeguard_check_submitted",
        "command_id": command_id,
        "claim_key": claim_key,
    }

    # If saving the receipt fails, retain the claim and do not resubmit.
    client.put_object(
        Bucket=bucket,
        Key=receipt_key,
        Body=json.dumps(
            receipt,
            sort_keys=True,
            allow_nan=False,
        ).encode("utf-8"),
        ContentType="application/json",
        IfNoneMatch="*",
    )

    return {
        **receipt,
        "receipt_key": receipt_key,
    }

def retrieve_audio_safeguard_check(
    *,
    run_id,
    execution_arn,
    owner_token,
    lock_etag,
    bucket=None,
    client=None,
    ssm_client=None,
):
    """Read one saved command; never submit commands or modify EC2."""
    from datetime import timedelta
    from uuid import UUID

    bucket = bucket or os.environ.get("MIP_S3_BUCKET")
    if not isinstance(bucket, str) or not bucket.strip():
        raise ValueError("MIP_S3_BUCKET must be configured")

    config = Config(
        retries={"total_max_attempts": 1},
        connect_timeout=10,
        read_timeout=30,
    )
    if client is None:
        client = boto3.client("s3", region_name=AUDIO_REGION, config=config)

    _verify_worker_lock(
        run_id=run_id,
        execution_arn=execution_arn,
        owner_token=owner_token,
        lock_etag=lock_etag,
        bucket=bucket,
        client=client,
    )
    storage = S3RunStorage(bucket=bucket, client=client)
    identity = {
        "run_id": run_id,
        "execution_arn": execution_arn,
        "instance_id": AUDIO_INSTANCE_ID,
        "owner_token": owner_token,
    }

    def timestamp(value):
        if not isinstance(value, str):
            raise ValueError("Expected an ISO timestamp")
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError("Safeguard timestamps must include a timezone")
        return parsed

    start_claim = storage.read_json(run_id, "audio_instance_start_claim.json")
    claim = storage.read_json(run_id, "audio_safeguard_submission_claim.json")
    receipt = storage.read_json(run_id, "audio_safeguard_submission.json")
    if not all(isinstance(item, dict) for item in (start_claim, claim, receipt)):
        raise ValueError("Expected saved safeguard records")

    for record in (start_claim, claim):
        if (
            any(record.get(key) != value for key, value in identity.items())
            or record.get("automatic_retry_allowed") is not False
        ):
            raise ValueError("Saved safeguard ownership does not match")

    probe_token = claim.get("probe_token")
    expected_request = build_audio_safeguard_request(
        run_id=run_id,
        bucket=bucket,
        probe_token=probe_token,
    )
    expected_claim = {
        **identity,
        "probe_token": probe_token,
        "requested_at": claim.get("requested_at"),
        "request_sha256": json_sha256(expected_request),
        "output_prefix": expected_request["OutputS3KeyPrefix"],
        "automatic_retry_allowed": False,
        "generation_requested": False,
    }
    if claim != expected_claim or claim.get("generation_requested") is not False:
        raise ValueError("Saved safeguard claim does not match its request")

    command_id = receipt.get("command_id")
    if not isinstance(command_id, str) or str(UUID(command_id)) != command_id:
        raise ValueError("Expected a canonical saved SSM command UUID")

    claim_key = S3RunStorage._key(
        run_id, "audio_safeguard_submission_claim.json"
    )
    expected_receipt = {
        **claim,
        "status": "audio_safeguard_check_submitted",
        "command_id": command_id,
        "claim_key": claim_key,
    }
    if (
        receipt != expected_receipt
        or receipt.get("automatic_retry_allowed") is not False
        or receipt.get("generation_requested") is not False
    ):
        raise ValueError("Saved safeguard receipt does not match its claim")

    start_requested = timestamp(start_claim.get("requested_at"))
    requested = timestamp(claim["requested_at"])
    now = datetime.now(timezone.utc)
    if not start_requested <= requested <= now:
        raise ValueError("Invalid start or safeguard submission timing")
    if now - requested >= timedelta(seconds=600):
        # This does not mean the SSM command was cancelled.
        raise TimeoutError("Safeguard polling deadline exceeded; reconcile SSM")

    result_base = {
        **identity,
        "command_id": command_id,
        "probe_token": probe_token,
        "generation_requested": False,
    }
    if ssm_client is None:
        ssm_client = boto3.client(
            "ssm", region_name=AUDIO_REGION, config=config
        )
    try:
        invocation = ssm_client.get_command_invocation(
            CommandId=command_id,
            InstanceId=AUDIO_INSTANCE_ID,
        )
    except ClientError as error:
        if error.response.get("Error", {}).get("Code") != "InvocationDoesNotExist":
            raise
        return {
            **result_base,
            "status": "audio_safeguard_check_pending",
            "reason": "invocation_not_visible",
        }

    expected_invocation = {
        "CommandId": command_id,
        "InstanceId": AUDIO_INSTANCE_ID,
        "DocumentName": "AWS-RunShellScript",
        "Comment": expected_request["Comment"],
    }
    if any(
        invocation.get(key) != value
        for key, value in expected_invocation.items()
    ):
        raise ValueError("SSM invocation does not match the saved submission")

    status = invocation.get("Status")
    if status in {"Pending", "InProgress", "Delayed", "Cancelling"}:
        return {
            **result_base,
            "status": "audio_safeguard_check_pending",
            "reason": status,
        }
    if status not in {"Success", "Failed", "TimedOut", "Cancelled"}:
        raise ValueError(f"Unexpected safeguard invocation status: {status}")

    # Save terminal evidence before interpreting stdout. Exclude metadata
    # and output URLs, which are not part of the stable execution result.
    fields = (
        "CommandId", "InstanceId", "DocumentName", "Comment", "PluginName",
        "Status", "StatusDetails", "ResponseCode",
        "ExecutionStartDateTime", "ExecutionEndDateTime",
        "StandardOutputContent", "StandardErrorContent",
    )
    evidence = {
        **result_base,
        "start_requested_at": start_claim["requested_at"],
        "submitted_at": claim["requested_at"],
        "invocation": {key: invocation.get(key) for key in fields},
    }
    evidence_filename = "audio_safeguard_invocation.json"
    storage.preserve_json(run_id, evidence_filename, evidence)
    evidence_key = S3RunStorage._key(run_id, evidence_filename)

    if status != "Success":
        result = {
            **result_base,
            "status": "audio_safeguard_check_failed",
            "invocation_status": status,
            "evidence_key": evidence_key,
        }
    else:
        if (
            invocation.get("StatusDetails") != "Success"
            or type(invocation.get("ResponseCode")) is not int
            or invocation["ResponseCode"] != 0
            or invocation.get("StandardErrorContent") != ""
        ):
            raise ValueError("SSM did not confirm a clean safeguard execution")

        probe = validate_audio_safeguard_output(
            output=invocation.get("StandardOutputContent"),
            probe_token=probe_token,
        )
        started = timestamp(invocation.get("ExecutionStartDateTime"))
        finished = timestamp(invocation.get("ExecutionEndDateTime"))
        now = datetime.now(timezone.utc)
        if not requested <= started <= finished <= now:
            raise ValueError("Safeguard execution timing does not match submission")

        # Uptime was sampled during the invocation. This rejects evidence
        # from a boot predating our start request, allowing 5s clock skew.
        latest_possible_boot = finished - timedelta(
            seconds=probe["uptime_seconds"]
        )
        if latest_possible_boot < start_requested - timedelta(seconds=5):
            raise ValueError("Safeguard output appears to come from an older boot")

        # Conservatively age the sample from invocation start.
        maximum_current_uptime = (
            probe["uptime_seconds"] + (now - started).total_seconds()
        )
        if maximum_current_uptime * 1_000_000 >= probe["shutdown_deadline_monotonic_us"]:
            raise ValueError("Safeguard shutdown deadline has already elapsed")

        result = {
            **result_base,
            **probe,
            "start_requested_at": start_claim["requested_at"],
            "submitted_at": claim["requested_at"],
            "execution_started_at": started.isoformat(),
            "execution_finished_at": finished.isoformat(),
            "evidence_key": evidence_key,
        }

    result_filename = "audio_safeguard_result.json"
    storage.preserve_json(run_id, result_filename, result)
    return {
        **result,
        "result_key": S3RunStorage._key(run_id, result_filename),
    }

def prepare_audio_handoff(
    *,
    run_id,
    execution_arn,
    owner_token,
    lock_etag,
    bucket=None,
    client=None,
    ssm_client=None,
):
    """Preserve a verified handoff descriptor without submitting it."""
    from cloud.audio_handoff import build_audio_handoff_command

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
    safeguards = retrieve_audio_safeguard_check(
        run_id=run_id,
        execution_arn=execution_arn,
        owner_token=owner_token,
        lock_etag=lock_etag,
        bucket=bucket,
        client=client,
        ssm_client=ssm_client,
    )
    if safeguards["status"] != "audio_safeguards_verified":
        return safeguards

    storage = S3RunStorage(bucket=bucket, client=client)
    prepared = prepare_audio_run(run_id=run_id, storage=storage)
    payload = {
        "run_id": run_id,
        "execution_arn": execution_arn,
        "instance_id": AUDIO_INSTANCE_ID,
        "owner_token": owner_token,
        "lock_etag": lock_etag,
        "bucket": bucket,
        "region": AUDIO_REGION,
        "boot_id": safeguards["boot_id"],
        "handoff_token": safeguards["probe_token"],
        "job_sha256": prepared["job_sha256"],
        "script_sha256": prepared["script_sha256"],
    }
    # Compile and validate the self-contained command before preserving.
    build_audio_handoff_command(payload)
    preservation = storage.preserve_json(run_id, "audio_handoff.json", payload)
    return {
        "status": "audio_handoff_prepared",
        **payload,
        "handoff_key": S3RunStorage._key(run_id, "audio_handoff.json"),
        "handoff_sha256": json_sha256(payload),
        "preservation": preservation,
        "generation_requested": False,
    }