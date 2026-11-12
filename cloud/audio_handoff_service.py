"""Submit and verify one prepared audio handoff through SSM."""

from datetime import datetime, timezone, timedelta
import json
import os
from uuid import UUID

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

from cloud import audio_handoff as handoff
from cloud import audio_service as audio
from cloud.acceptance import json_sha256, load_accepted_script
from cloud.storage import S3RunStorage


def _aws_client(name):
    return boto3.client(
        name, region_name=audio.AUDIO_REGION,
        config=Config(
            retries={"total_max_attempts": 1},
            connect_timeout=10,
            read_timeout=30,
        ),
    )


def _context(
    run_id, execution_arn, owner_token, lock_etag, bucket, client,
    *, request_required=True,
):
    bucket = bucket or os.environ.get("MIP_S3_BUCKET")
    if not isinstance(bucket, str) or not bucket.strip():
        raise ValueError("MIP_S3_BUCKET must be configured")
    client = client if client is not None else _aws_client("s3")
    audio._verify_worker_lock(
        run_id=run_id, execution_arn=execution_arn,
        owner_token=owner_token, lock_etag=lock_etag,
        bucket=bucket, client=client,
    )
    storage = S3RunStorage(bucket=bucket, client=client)
    payload = handoff.validate_handoff(
        storage.read_json(run_id, "audio_handoff.json")
    )
    expected = {
        "run_id": run_id, "execution_arn": execution_arn,
        "owner_token": owner_token, "lock_etag": lock_etag, "bucket": bucket,
    }
    if any(payload[key] != value for key, value in expected.items()):
        raise ValueError("Prepared handoff does not match current ownership")
    if not request_required:
        return storage, payload, None
    request = {
        "InstanceIds": [audio.AUDIO_INSTANCE_ID],
        "DocumentName": "AWS-RunShellScript",
        "TimeoutSeconds": 60,
        "Comment": f'MIP handoff {payload["handoff_token"]}',
        "Parameters": {
            "commands": [handoff.build_audio_handoff_command(payload)],
            "executionTimeout": ["300"],
            "workingDirectory": ["/opt/mip/app"],
        },
        "OutputS3Region": audio.AUDIO_REGION,
        "OutputS3BucketName": bucket,
        "OutputS3KeyPrefix": (
            f'runs/{run_id}/audio/handoff/{payload["handoff_token"]}'
        ),
    }
    return storage, payload, request


def _claim(payload, request, requested_at):
    return {
        **payload,
        "handoff_sha256": json_sha256(payload),
        "request_sha256": json_sha256(request),
        "requested_at": requested_at,
        "automatic_retry_allowed": False,
        "generation_requested": False,
    }


def _put_once(storage, run_id, filename, value):
    storage.client.put_object(
        Bucket=storage.bucket,
        Key=S3RunStorage._key(run_id, filename),
        Body=json.dumps(value, sort_keys=True, allow_nan=False).encode("utf-8"),
        ContentType="application/json",
        IfNoneMatch="*",
    )


def _command_id(value):
    if not isinstance(value, str) or str(UUID(value)) != value:
        raise ValueError("Expected a canonical saved handoff command UUID")
    return value


def _timestamp(value):
    if not isinstance(value, str):
        raise ValueError("Expected a handoff timestamp")
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("Handoff timestamp must include a timezone")
    return parsed


def submit_audio_handoff(
    *, run_id, execution_arn, owner_token, lock_etag,
    bucket=None, client=None, ssm_client=None,
):
    """Claim once, submit once, and preserve the confirmed command ID."""
    storage, payload, request = _context(
        run_id, execution_arn, owner_token, lock_etag, bucket, client
    )
    ssm_client = ssm_client if ssm_client is not None else _aws_client("ssm")
    safeguards = audio.retrieve_audio_safeguard_check(
        run_id=run_id, execution_arn=execution_arn,
        owner_token=owner_token, lock_etag=lock_etag,
        bucket=storage.bucket, client=storage.client, ssm_client=ssm_client,
    )
    if safeguards["status"] != "audio_safeguards_verified":
        return safeguards
    if (
        safeguards["boot_id"] != payload["boot_id"]
        or safeguards["probe_token"] != payload["handoff_token"]
    ):
        raise ValueError("Prepared handoff does not match verified safeguards")

    job = storage.read_json(run_id, "audio_job.json")
    accepted = load_accepted_script(run_id=run_id, storage=storage)
    if (
        json_sha256(job) != payload["job_sha256"]
        or accepted["acceptance"]["script_sha256"] != payload["script_sha256"]
    ):
        raise ValueError("Prepared handoff job or accepted script changed")

    claim = _claim(payload, request, datetime.now(timezone.utc).isoformat())
    claim_key = S3RunStorage._key(run_id, "audio_handoff_submission_claim.json")
    _put_once(storage, run_id, "audio_handoff_submission_claim.json", claim)

    submitted = ssm_client.send_command(**request).get("Command", {})
    try:
        command_id = _command_id(submitted.get("CommandId"))
    except (ValueError, TypeError, AttributeError) as error:
        raise RuntimeError("Handoff submission uncertain; reconcile SSM") from error
    if (
        submitted.get("InstanceIds") != [audio.AUDIO_INSTANCE_ID]
        or submitted.get("DocumentName") != request["DocumentName"]
        or submitted.get("Comment") != request["Comment"]
    ):
        raise RuntimeError("Handoff submission uncertain; reconcile SSM")

    receipt = {
        **claim,
        "status": "audio_handoff_submitted",
        "command_id": command_id,
        "claim_key": claim_key,
    }
    _put_once(storage, run_id, "audio_handoff_submission.json", receipt)
    return {
        **receipt,
        "receipt_key": S3RunStorage._key(run_id, "audio_handoff_submission.json"),
    }


def _output(text, payload, *, startup=False):
    if not isinstance(text, str) or not 0 < len(text) <= 4096:
        raise ValueError("Missing or oversized handoff output")

    def unique_fields(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("Duplicate handoff output field")
            result[key] = value
        return result

    def reject_constant(value):
        raise ValueError(f"Invalid JSON constant: {value}")

    result = json.loads(
        text, object_pairs_hook=unique_fields, parse_constant=reject_constant
    )
    expected = {
        **payload,
        "status": "audio_job_handed_off",
        "bundle_sha256": handoff.BUNDLE_SHA256,
        "installed_versions": handoff.EXPECTED_VERSIONS,
        "generation_requested": False,
    }
    if startup:
        expected.update(
            status="audio_service_start_acknowledged",
            startup_claim_key=S3RunStorage._key(
                payload["run_id"], "audio_service_start_execution_claim.json"
            ),
            generation_requested=True,
        )
    if (
        not isinstance(result, dict)
        or result != expected
        or result.get("generation_requested") is not expected["generation_requested"]
    ):
        raise ValueError("Audio command output does not match the prepared job")
    return result


def retrieve_audio_handoff(
    *, run_id, execution_arn, owner_token, lock_etag,
    bucket=None, client=None, ssm_client=None,
):
    """Poll the saved command only; never submit or start audio."""
    storage, payload, request = _context(
        run_id, execution_arn, owner_token, lock_etag, bucket, client
    )
    claim = storage.read_json(run_id, "audio_handoff_submission_claim.json")
    receipt = storage.read_json(run_id, "audio_handoff_submission.json")
    if not isinstance(claim, dict) or not isinstance(receipt, dict):
        raise ValueError("Expected saved handoff submission records")
    if (
        claim != _claim(payload, request, claim.get("requested_at"))
        or claim.get("automatic_retry_allowed") is not False
        or claim.get("generation_requested") is not False
    ):
        raise ValueError("Handoff claim does not match the prepared request")
    command_id = _command_id(receipt.get("command_id"))
    if (
        receipt != {
            **claim,
            "status": "audio_handoff_submitted",
            "command_id": command_id,
            "claim_key": S3RunStorage._key(
                run_id, "audio_handoff_submission_claim.json"
            ),
        }
        or receipt.get("automatic_retry_allowed") is not False
        or receipt.get("generation_requested") is not False
    ):
        raise ValueError("Handoff receipt does not match its claim")
    requested = _timestamp(claim["requested_at"])
    now = datetime.now(timezone.utc)
    if requested > now:
        raise ValueError("Handoff submission timestamp is in the future")
    if now - requested >= timedelta(seconds=600):
        raise TimeoutError("Handoff polling deadline exceeded; reconcile SSM")

    base = {**payload, "command_id": command_id, "generation_requested": False}
    ssm_client = ssm_client if ssm_client is not None else _aws_client("ssm")
    try:
        invocation = ssm_client.get_command_invocation(
            CommandId=command_id, InstanceId=audio.AUDIO_INSTANCE_ID
        )
    except ClientError as error:
        if error.response.get("Error", {}).get("Code") != "InvocationDoesNotExist":
            raise
        return {**base, "status": "audio_handoff_pending", "reason": "not_visible"}

    expected = {
        "CommandId": command_id,
        "InstanceId": audio.AUDIO_INSTANCE_ID,
        "DocumentName": request["DocumentName"],
        "Comment": request["Comment"],
    }
    if any(invocation.get(key) != value for key, value in expected.items()):
        raise ValueError("SSM handoff invocation identity does not match")
    status = invocation.get("Status")
    if status in {"Pending", "InProgress", "Delayed", "Cancelling"}:
        return {**base, "status": "audio_handoff_pending", "reason": status}
    if status not in {"Success", "Failed", "TimedOut", "Cancelled"}:
        raise ValueError(f"Unexpected handoff invocation status: {status}")

    fields = (
        "CommandId", "InstanceId", "DocumentName", "Comment", "PluginName",
        "Status", "StatusDetails", "ResponseCode",
        "ExecutionStartDateTime", "ExecutionEndDateTime",
        "StandardOutputContent", "StandardErrorContent",
    )
    evidence_name = "audio_handoff_invocation.json"
    storage.preserve_json(run_id, evidence_name, {
        **base,
        "submitted_at": claim["requested_at"],
        "invocation": {key: invocation.get(key) for key in fields},
    })
    evidence_key = S3RunStorage._key(run_id, evidence_name)
    if status != "Success":
        result = {
            **base, "status": "audio_handoff_failed",
            "invocation_status": status, "evidence_key": evidence_key,
        }
    else:
        if (
            invocation.get("StatusDetails") != "Success"
            or type(invocation.get("ResponseCode")) is not int
            or invocation["ResponseCode"] != 0
            or invocation.get("StandardErrorContent") != ""
        ):
            raise ValueError("SSM did not confirm a clean handoff execution")
        output = _output(invocation.get("StandardOutputContent"), payload)
        started = _timestamp(invocation.get("ExecutionStartDateTime"))
        finished = _timestamp(invocation.get("ExecutionEndDateTime"))
        if not requested <= started <= finished <= datetime.now(timezone.utc):
            raise ValueError("Handoff execution timing does not match submission")
        result = {
            **base, **output,
            "execution_started_at": started.isoformat(),
            "execution_finished_at": finished.isoformat(),
            "evidence_key": evidence_key,
        }
    storage.preserve_json(run_id, "audio_handoff_result.json", result)
    return {
        **result,
        "result_key": S3RunStorage._key(run_id, "audio_handoff_result.json"),
    }

def submit_audio_service_start(
    *, run_id, execution_arn, owner_token, lock_etag,
    bucket=None, client=None, ssm_client=None,
):
    """Verify the handoff, claim once, and submit one startup command."""
    storage, payload, request = _context(
        run_id, execution_arn, owner_token, lock_etag, bucket, client
    )
    ssm_client = ssm_client if ssm_client is not None else _aws_client("ssm")
    verified = retrieve_audio_handoff(
        run_id=run_id, execution_arn=execution_arn,
        owner_token=owner_token, lock_etag=lock_etag,
        bucket=storage.bucket, client=storage.client, ssm_client=ssm_client,
    )
    if verified["status"] != "audio_job_handed_off":
        return verified
    if (
        any(verified.get(key) != value for key, value in payload.items())
        or verified.get("generation_requested") is not False
    ):
        raise ValueError("Verified handoff does not match startup ownership")

    job = storage.read_json(run_id, "audio_job.json")
    accepted = load_accepted_script(run_id=run_id, storage=storage)
    if (
        json_sha256(job) != payload["job_sha256"]
        or accepted["acceptance"]["script_sha256"] != payload["script_sha256"]
    ):
        raise ValueError("Startup job or accepted script changed")

    try:
        storage.client.head_object(
            Bucket=storage.bucket,
            Key=S3RunStorage._key(run_id, "audio_submission_claim.json"),
        )
    except ClientError as error:
        if error.response.get("ResponseMetadata", {}).get("HTTPStatusCode") != 404:
            raise
    else:
        raise ValueError("This run already has a permanent audio claim")

    request["Comment"] = f'MIP service start {payload["handoff_token"]}'
    request["Parameters"] = {
        **request["Parameters"],
        "commands": [handoff.build_audio_service_start_command(payload)],
    }
    request["OutputS3KeyPrefix"] = (
        f'runs/{run_id}/audio/service-start/{payload["handoff_token"]}'
    )

    # Preserve the exact command for later verification and recovery.
    request_name = "audio_service_start_request.json"
    storage.preserve_json(run_id, request_name, request)

    claim = {
        **payload,
        "handoff_sha256": json_sha256(payload),
        "handoff_command_id": _command_id(verified["command_id"]),
        "request_key": S3RunStorage._key(run_id, request_name),
        "request_sha256": json_sha256(request),
        "requested_at": datetime.now(timezone.utc).isoformat(),
        "automatic_retry_allowed": False,
        "generation_requested": True,
    }
    claim_name = "audio_service_start_submission_claim.json"
    claim_key = S3RunStorage._key(run_id, claim_name)
    _put_once(storage, run_id, claim_name, claim)

    response = ssm_client.send_command(**request)
    submitted = response.get("Command") if isinstance(response, dict) else None
    if not isinstance(submitted, dict):
        raise RuntimeError("Service startup submission uncertain; reconcile SSM")
    try:
        command_id = _command_id(submitted.get("CommandId"))
    except (ValueError, TypeError, AttributeError) as error:
        raise RuntimeError(
            "Service startup submission uncertain; reconcile SSM"
        ) from error
    if (
        submitted.get("InstanceIds") != [audio.AUDIO_INSTANCE_ID]
        or submitted.get("DocumentName") != request["DocumentName"]
        or submitted.get("Comment") != request["Comment"]
    ):
        raise RuntimeError("Service startup submission uncertain; reconcile SSM")

    receipt = {
        **claim,
        "status": "audio_service_start_submitted",
        "command_id": command_id,
        "claim_key": claim_key,
    }
    receipt_name = "audio_service_start_submission.json"
    _put_once(storage, run_id, receipt_name, receipt)
    return {
        **receipt,
        "receipt_key": S3RunStorage._key(run_id, receipt_name),
    }

def retrieve_audio_service_start(
    *, run_id, execution_arn, owner_token, lock_etag,
    bucket=None, client=None, ssm_client=None,
):
    """Poll the saved startup command without submitting or restarting."""
    storage, payload, _ = _context(
        run_id, execution_arn, owner_token, lock_etag, bucket, client,
        request_required=False,
    )
    request_name = "audio_service_start_request.json"
    claim_name = "audio_service_start_submission_claim.json"
    request = storage.read_json(run_id, request_name)
    claim = storage.read_json(run_id, claim_name)
    receipt = storage.read_json(run_id, "audio_service_start_submission.json")
    if not all(isinstance(value, dict) for value in (request, claim, receipt)):
        raise ValueError("Expected saved startup request, claim and receipt")

    expected_claim = {
        **payload,
        "handoff_sha256": json_sha256(payload),
        "handoff_command_id": _command_id(claim.get("handoff_command_id")),
        "request_key": S3RunStorage._key(run_id, request_name),
        "request_sha256": json_sha256(request),
        "requested_at": claim.get("requested_at"),
        "automatic_retry_allowed": False,
        "generation_requested": True,
    }
    if (
        claim != expected_claim
        or claim.get("automatic_retry_allowed") is not False
        or claim.get("generation_requested") is not True
    ):
        raise ValueError("Startup claim does not match its saved request")

    command_id = _command_id(receipt.get("command_id"))
    if (
        receipt != {
            **claim,
            "status": "audio_service_start_submitted",
            "command_id": command_id,
            "claim_key": S3RunStorage._key(run_id, claim_name),
        }
        or receipt.get("automatic_retry_allowed") is not False
        or receipt.get("generation_requested") is not True
    ):
        raise ValueError("Startup receipt does not match its claim")

    expected_request_fields = {
        "InstanceIds": [audio.AUDIO_INSTANCE_ID],
        "DocumentName": "AWS-RunShellScript",
        "Comment": f'MIP service start {payload["handoff_token"]}',
        "OutputS3Region": audio.AUDIO_REGION,
        "OutputS3BucketName": storage.bucket,
        "OutputS3KeyPrefix": (
            f'runs/{run_id}/audio/service-start/{payload["handoff_token"]}'
        ),
    }
    if any(
        request.get(key) != value
        for key, value in expected_request_fields.items()
    ):
        raise ValueError("Saved startup request identity does not match")

    requested = _timestamp(claim["requested_at"])
    now = datetime.now(timezone.utc)
    if requested > now:
        raise ValueError("Startup submission timestamp is in the future")
    if now - requested >= timedelta(seconds=600):
        raise TimeoutError("Startup polling deadline exceeded; reconcile SSM")

    base = {**payload, "command_id": command_id, "generation_requested": True}
    ssm_client = ssm_client if ssm_client is not None else _aws_client("ssm")
    try:
        invocation = ssm_client.get_command_invocation(
            CommandId=command_id, InstanceId=audio.AUDIO_INSTANCE_ID
        )
    except ClientError as error:
        if error.response.get("Error", {}).get("Code") != "InvocationDoesNotExist":
            raise
        return {
            **base, "status": "audio_service_start_pending",
            "reason": "not_visible",
        }

    expected_identity = {
        "CommandId": command_id,
        "InstanceId": audio.AUDIO_INSTANCE_ID,
        "DocumentName": request["DocumentName"],
        "Comment": request["Comment"],
    }
    if any(
        invocation.get(key) != value
        for key, value in expected_identity.items()
    ):
        raise ValueError("SSM startup invocation identity does not match")

    status = invocation.get("Status")
    if status in {"Pending", "InProgress", "Delayed", "Cancelling"}:
        return {
            **base, "status": "audio_service_start_pending", "reason": status,
        }
    if status not in {"Success", "Failed", "TimedOut", "Cancelled"}:
        raise ValueError(f"Unexpected startup invocation status: {status}")

    fields = (
        "CommandId", "InstanceId", "DocumentName", "Comment", "PluginName",
        "Status", "StatusDetails", "ResponseCode",
        "ExecutionStartDateTime", "ExecutionEndDateTime",
        "StandardOutputContent", "StandardErrorContent",
    )
    evidence_name = "audio_service_start_invocation.json"
    storage.preserve_json(run_id, evidence_name, {
        **base,
        "submitted_at": claim["requested_at"],
        "invocation": {key: invocation.get(key) for key in fields},
    })
    evidence_key = S3RunStorage._key(run_id, evidence_name)

    if status != "Success":
        result = {
            **base,
            "status": "audio_service_start_failed",
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
            raise ValueError("SSM did not confirm a clean startup execution")
        output = _output(
            invocation.get("StandardOutputContent"), payload, startup=True
        )
        started = _timestamp(invocation.get("ExecutionStartDateTime"))
        finished = _timestamp(invocation.get("ExecutionEndDateTime"))
        if not requested <= started <= finished <= datetime.now(timezone.utc):
            raise ValueError("Startup execution timing does not match submission")
        result = {
            **base, **output,
            "execution_started_at": started.isoformat(),
            "execution_finished_at": finished.isoformat(),
            "evidence_key": evidence_key,
        }

    result_name = "audio_service_start_result.json"
    storage.preserve_json(run_id, result_name, result)
    return {
        **result,
        "result_key": S3RunStorage._key(run_id, result_name),
    }