"""Prepare per-run audio configuration without starting the worker."""

from cloud.acceptance import json_sha256, load_accepted_script
from cloud.storage import S3RunStorage


AUDIO_REGION = "us-east-2"

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