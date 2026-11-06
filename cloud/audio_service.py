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
    started = response.get("StartingInstances", [])
    if (
        len(started) != 1
        or started[0].get("InstanceId") != AUDIO_INSTANCE_ID
        or started[0].get("PreviousState", {}).get("Name") != "stopped"
        or started[0].get("CurrentState", {}).get("Name")
        not in {"pending", "running"}
    ):
        raise RuntimeError(
            "EC2 start outcome uncertain; inspect before recovery"
        )

    return {
        "status": "audio_instance_start_requested",
        "run_id": run_id,
        "execution_arn": execution_arn,
        "instance_id": AUDIO_INSTANCE_ID,
        "requested_at": requested_at,
        "start_claim_key": claim_key,
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