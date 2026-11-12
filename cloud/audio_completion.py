"""Read and verify audio completion evidence without starting generation."""

from datetime import datetime, timezone
import math
import re
import hashlib
import json

from botocore.exceptions import ClientError
from cloud.acceptance import json_sha256


def _require(condition, message):
    if not condition:
        raise ValueError(message)


def _timestamp(value):
    _require(isinstance(value, str), "Expected an audio completion timestamp")
    parsed = datetime.fromisoformat(value)
    _require(parsed.utcoffset() is not None, "Completion timestamp needs a timezone")
    return parsed


def _validate_audio_claim(*, run_id, job, job_sha256, script_sha256, claim):
    """Bind the permanent claim to the expected job before using its attempt ID."""
    _require(
        isinstance(run_id, str)
        and re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}-[0-9]{3}", run_id),
        "Invalid completion run_id",
    )
    _require(
        isinstance(job, dict) and job.get("run_id") == run_id
        and isinstance(job.get("voices"), dict)
        and set(job["voices"]) == {"HOST_A", "HOST_B"}
        and json_sha256(job) == job_sha256,
        "Completion job does not match the prepared job",
    )
    _require(
        isinstance(script_sha256, str)
        and re.fullmatch(r"[0-9a-f]{64}", script_sha256),
        "Invalid expected script hash",
    )
    _require(
        isinstance(claim, dict),
        "Expected an audio claim object",
    )
    attempt_id = claim.get("attempt_id")
    _require(
        isinstance(attempt_id, str) and re.fullmatch(r"[0-9a-f]{32}", attempt_id),
        "Invalid audio attempt_id",
    )
    expected_claim = {
        "run_id": run_id,
        "attempt_id": attempt_id,
        "status": "audio_submission_claimed",
        "script_sha256": script_sha256,
        "voices": job["voices"],
        "automatic_retry_allowed": False,
    }
    _require(
        claim == expected_claim and claim.get("automatic_retry_allowed") is False,
        "Audio claim does not match the prepared job and script",
    )
    prefix = f"runs/{run_id}/audio/attempts/{attempt_id}/"
    return attempt_id, prefix


def validate_audio_success(
    *, run_id, job, job_sha256, script_sha256,
    startup_requested_at, claim, success,
):
    """Verify the manifest; artifact bytes must still be checked separately."""
    attempt_id, prefix = _validate_audio_claim(
        run_id=run_id, job=job, job_sha256=job_sha256,
        script_sha256=script_sha256, claim=claim,
    )
    _require(isinstance(success, dict), "Expected an audio success object")
    expected = {
        "run_id": run_id,
        "attempt_id": attempt_id,
        "s3_prefix": prefix,
        "status": "audio_completed",
        "script_sha256": script_sha256,
        "mp3_key": prefix + "render/episode.mp3",
        "wav_key": prefix + "render/episode.wav",
    }
    _require(
        all(success.get(key) == value for key, value in expected.items())
        and success.get("automatic_retry_allowed") is False
        and success.get("claim_confirmed") is True,
        "Audio success marker does not match its permanent claim",
    )
    duration = success.get("duration_seconds")
    _require(
        type(duration) in {int, float} and math.isfinite(duration) and duration > 0,
        "Invalid completed episode duration",
    )
    started = _timestamp(success.get("started_at"))
    finished = _timestamp(success.get("finished_at"))
    _require(
        _timestamp(startup_requested_at) <= started <= finished
        <= datetime.now(timezone.utc),
        "Audio completion timing does not match this startup",
    )

    inventory = success.get("artifacts")
    _require(isinstance(inventory, list) and bool(inventory), "Missing inventory")
    indexed = {}
    for artifact in inventory:
        _require(
            isinstance(artifact, dict) and set(artifact) == {"key", "sha256", "bytes"},
            "Invalid artifact record",
        )
        key = artifact["key"]
        _require(
            isinstance(key, str) and key.startswith(prefix),
            "Artifact is outside the claimed attempt",
        )
        relative = key[len(prefix):]
        _require(
            re.fullmatch(r"[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)*", relative)
            and all(part not in {".", ".."} for part in relative.split("/"))
            and relative not in indexed,
            "Invalid or duplicate artifact path",
        )
        _require(
            isinstance(artifact["sha256"], str)
            and re.fullmatch(r"[0-9a-f]{64}", artifact["sha256"])
            and type(artifact["bytes"]) is int and artifact["bytes"] >= 0,
            "Invalid artifact hash or byte count",
        )
        indexed[relative] = dict(artifact)

    required = (
        "audio_job.json", "verified_script_acceptance.json", "job_result.json",
        "render/script.md", "render/render_result.json",
        "render/episode.mp3", "render/episode.wav",
    )
    _require(
        all(name in indexed and indexed[name]["bytes"] > 0 for name in required),
        "Required audio outputs or evidence are missing or empty",
    )
    _require(
        indexed["render/script.md"]["sha256"] == script_sha256,
        "Rendered script does not match the accepted script",
    )
    return {
        **expected,
        "status": "audio_success_manifest_verified",
        "job_sha256": job_sha256,
        "duration_seconds": duration,
        "started_at": started.isoformat(),
        "finished_at": finished.isoformat(),
        "success_key": prefix + "audio_success.json",
        "artifacts": {name: indexed[name] for name in required},
    }

_JSON_LIMIT = 2 * 1024 * 1024


def _decode_json(raw):
    _require(len(raw) <= _JSON_LIMIT, "Audio evidence JSON is too large")

    def unique_fields(pairs):
        result = {}
        for key, value in pairs:
            _require(key not in result, "Duplicate audio evidence JSON field")
            result[key] = value
        return result

    def reject_constant(value):
        raise ValueError(f"Invalid audio evidence JSON constant: {value}")

    value = json.loads(
        raw, object_pairs_hook=unique_fields, parse_constant=reject_constant
    )
    _require(isinstance(value, dict), "Audio evidence must be a JSON object")
    return value


def _optional_s3_json(client, bucket, key):
    try:
        response = client.get_object(Bucket=bucket, Key=key)
    except ClientError as error:
        details = error.response
        if (
            details.get("ResponseMetadata", {}).get("HTTPStatusCode") == 404
            and details.get("Error", {}).get("Code") in {"NoSuchKey", "404", "NotFound"}
        ):
            return None
        raise
    with response["Body"] as stream:
        return _decode_json(stream.read(_JSON_LIMIT + 1))


def _read_verified_artifact(client, bucket, artifact, *, keep_json):
    response = client.get_object(Bucket=bucket, Key=artifact["key"])
    digest, count, pieces = hashlib.sha256(), 0, []
    with response["Body"] as stream:
        _require(
            response.get("ContentLength") == artifact["bytes"],
            "S3 artifact size differs from the completion manifest",
        )
        if keep_json:
            _require(artifact["bytes"] <= _JSON_LIMIT, "Audio evidence JSON is too large")
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            count += len(chunk)
            _require(count <= artifact["bytes"], "Artifact exceeds its declared size")
            digest.update(chunk)
            if keep_json:
                pieces.append(chunk)
    _require(
        count == artifact["bytes"] and digest.hexdigest() == artifact["sha256"],
        "S3 artifact bytes do not match the completion manifest",
    )
    return _decode_json(b"".join(pieces)) if keep_json else None


def read_audio_completion(
    *, run_id, job, job_sha256, script_acceptance,
    startup_requested_at, client,
):
    """Read one claimed attempt; missing completion evidence is not failure.

    The caller supplies the verified prepared job and current script acceptance.
    Ownership, runtime deadlines and EC2 shutdown are handled by orchestration.
    """
    _require(
        isinstance(run_id, str)
        and re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}-[0-9]{3}", run_id),
        "Invalid completion run_id",
    )
    _require(
        isinstance(job, dict) and isinstance(job.get("bucket"), str)
        and bool(job["bucket"].strip()) and job.get("run_id") == run_id
        and json_sha256(job) == job_sha256,
        "Completion reader needs the verified prepared job",
    )
    _require(isinstance(script_acceptance, dict), "Expected verified script acceptance")
    script_sha256 = script_acceptance.get("script_sha256")
    bucket = job["bucket"]
    base = {"run_id": run_id, "job_sha256": job_sha256, "script_sha256": script_sha256}
    claim = _optional_s3_json(
        client, bucket, f"runs/{run_id}/audio_submission_claim.json"
    )
    if claim is None:
        return {
            **base, "status": "audio_completion_pending",
            "reason": "permanent_claim_missing",
        }
    attempt_id, prefix = _validate_audio_claim(
        run_id=run_id, job=job, job_sha256=job_sha256,
        script_sha256=script_sha256, claim=claim,
    )
    success = _optional_s3_json(client, bucket, prefix + "audio_success.json")
    if success is None:
        return {
            **base, "attempt_id": attempt_id,
            "status": "audio_completion_pending", "reason": "success_marker_missing",
        }
    manifest = validate_audio_success(
        run_id=run_id, job=job, job_sha256=job_sha256,
        script_sha256=script_sha256, startup_requested_at=startup_requested_at,
        claim=claim, success=success,
    )
    evidence = {}
    for name, artifact in manifest["artifacts"].items():
        value = _read_verified_artifact(
            client, bucket, artifact, keep_json=name.endswith(".json")
        )
        if value is not None:
            evidence[name] = value
    _require(
        json_sha256(evidence["audio_job.json"]) == job_sha256
        and evidence["audio_job.json"] == job,
        "Uploaded job does not match the prepared job",
    )
    _require(
        evidence["verified_script_acceptance.json"] == script_acceptance,
        "Uploaded script acceptance differs from current verified acceptance",
    )
    expected_report = {
        key: value for key, value in success.items()
        if key not in {"mp3_key", "wav_key", "artifacts"}
    }
    expected_report["status"] = "render_completed"
    report = evidence["job_result.json"]
    _require(
        report == expected_report and report.get("claim_confirmed") is True
        and report.get("automatic_retry_allowed") is False,
        "Uploaded job result disagrees with the success marker",
    )
    rendered = evidence["render/render_result.json"]
    lines = script_acceptance.get("dialogue_line_count")
    _require(
        type(lines) is int and lines > 0
        and rendered.get("status") == "render_completed"
        and rendered.get("script_sha256") == script_sha256
        and rendered.get("duration_seconds") == manifest["duration_seconds"]
        and type(rendered.get("completed_dialogue_lines")) is int
        and type(rendered.get("expected_dialogue_lines")) is int
        and rendered["completed_dialogue_lines"] == rendered["expected_dialogue_lines"] == lines,
        "Renderer evidence does not confirm the accepted episode",
    )
    return {
        **manifest, "status": "audio_completion_verified",
        "claim_sha256": json_sha256(claim),
        "success_sha256": json_sha256(success),
    }