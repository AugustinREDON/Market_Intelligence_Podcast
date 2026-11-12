"""Validate audio completion records without AWS calls or generation."""

from datetime import datetime, timezone
import math
import re

from cloud.acceptance import json_sha256


def _require(condition, message):
    if not condition:
        raise ValueError(message)


def _timestamp(value):
    _require(isinstance(value, str), "Expected an audio completion timestamp")
    parsed = datetime.fromisoformat(value)
    _require(parsed.utcoffset() is not None, "Completion timestamp needs a timezone")
    return parsed


def validate_audio_success(
    *, run_id, job, job_sha256, script_sha256,
    startup_requested_at, claim, success,
):
    """Verify the manifest; artifact bytes must still be checked separately."""
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
        isinstance(claim, dict) and isinstance(success, dict),
        "Expected audio claim and success objects",
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