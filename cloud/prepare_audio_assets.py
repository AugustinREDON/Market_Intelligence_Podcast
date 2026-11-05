"""Stage original MP3 voice references and prepare an audio job file."""

import argparse
import hashlib
import json
import os
from pathlib import Path

from dotenv import dotenv_values

from cloud.audio_job import validate_job
from cloud.storage import S3RunStorage


PROJECT_DIR = Path(__file__).resolve().parents[1]


def preserve_bytes(path, content):
    """Create a local file or confirm existing content is identical."""
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("xb") as stream:
            stream.write(content)
    except FileExistsError:
        if path.read_bytes() != content:
            raise ValueError(f"Existing file differs: {path}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-id", required=True)
    args = parser.parse_args()
    S3RunStorage._key(args.run_id, "script.md")

    settings = dotenv_values(PROJECT_DIR / ".env")
    staged_dir = PROJECT_DIR / "local_runs/audio/voice_assets/v1"
    voices = {}
    staged_files = []

    for speaker, variable, filename in (
        ("HOST_A", "HOST_A_VOICE_PATH", "host_a.mp3"),
        ("HOST_B", "HOST_B_VOICE_PATH", "host_b.mp3"),
    ):
        value = os.environ.get(variable, settings.get(variable))
        if not value:
            raise ValueError(f"Missing {variable}")

        source = Path(value).expanduser()
        if not source.is_absolute():
            source = PROJECT_DIR / source

        extension = source.suffix.lower()
        if extension not in {".wav", ".mp3"}:
            raise ValueError(f"{speaker} reference must be WAV or MP3")
        filename = f"{speaker.lower()}{extension}"
        content = source.read_bytes()
        if not content:
            raise ValueError(f"{speaker} reference is empty")

        voices[speaker] = {
            "key": f"assets/voices/v1/{filename}",
            "sha256": hashlib.sha256(content).hexdigest(),
        }
        staged_files.append((staged_dir / filename, content))

    job = validate_job({
        "run_id": args.run_id,
        "bucket": "mip-storage-340015815972-us-east-2",
        "region": "us-east-2",
        "voices": voices,
    })
    job_path = (
        PROJECT_DIR / "local_runs/audio" / args.run_id / "audio_job.json"
    )

    for destination, content in staged_files:
        preserve_bytes(destination, content)

    preserve_bytes(
        job_path,
        (json.dumps(job, indent=2) + "\n").encode("utf-8"),
    )

    print(json.dumps(job, indent=2))
    print(f"Voice files staged: {staged_dir}")
    print(f"Job configuration saved: {job_path}")


if __name__ == "__main__":
    main()