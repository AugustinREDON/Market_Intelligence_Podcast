"""Run one accepted-script audio job using private S3 artifacts."""

import argparse
import fcntl
import hashlib
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import boto3
from botocore.config import Config

from cloud.acceptance import load_accepted_script
from cloud.audio_renderer import render_episode
from cloud.storage import S3RunStorage


def file_sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def save_json(path, value):
    with Path(path).open("x", encoding="utf-8") as stream:
        json.dump(value, stream, indent=2, allow_nan=False)
        stream.write("\n")


def put_json(client, bucket, key, value):
    """Create once; never overwrite or automatically retry a write."""
    client.put_object(
        Bucket=bucket,
        Key=key,
        Body=json.dumps(value, indent=2, allow_nan=False).encode("utf-8"),
        ContentType="application/json",
        IfNoneMatch="*",
    )


def validate_job(job):
    if not isinstance(job, dict):
        raise ValueError("Audio job must be a JSON object")

    for field in ("bucket", "region"):
        if not isinstance(job.get(field), str) or not job[field].strip():
            raise ValueError(f"Audio job requires {field}")

    S3RunStorage._key(job.get("run_id"), "script.md")

    voices = job.get("voices")
    if not isinstance(voices, dict) or set(voices) != {"HOST_A", "HOST_B"}:
        raise ValueError("voices must contain HOST_A and HOST_B")

    for speaker, filename in (
        ("HOST_A", "host_a.mp3"),
        ("HOST_B", "host_b.mp3"),
    ):
        reference = voices[speaker]
        if not isinstance(reference, dict):
            raise ValueError(f"Invalid voice reference: {speaker}")
        key = reference.get("key")
        if not isinstance(key, str) or not re.fullmatch(
            rf"assets/voices/v[1-9][0-9]*/{speaker.lower()}\.(?:wav|mp3)",
            key,
        ):
            raise ValueError(f"Invalid versioned WAV/MP3 key: {speaker}")
        digest = reference.get("sha256")
        if not isinstance(digest, str) or not re.fullmatch(
            r"[0-9a-f]{64}", digest
        ):
            raise ValueError(f"Invalid voice SHA-256: {speaker}")

    return job


def execute_job(*, job, client, work_root):
    run_id = job["run_id"]
    bucket = job["bucket"]
    attempt_id = uuid4().hex
    attempt_dir = work_root / run_id / attempt_id
    attempt_dir.mkdir(parents=True, exist_ok=False)
    prefix = f"runs/{run_id}/audio/attempts/{attempt_id}/"

    report = {
        "run_id": run_id,
        "attempt_id": attempt_id,
        "s3_prefix": prefix,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "status": "preparing",
        "automatic_retry_allowed": False,
    }
    claimed = False

    try:
        save_json(attempt_dir / "audio_job.json", job)

        accepted = load_accepted_script(
            run_id=run_id,
            storage=S3RunStorage(bucket=bucket, client=client),
        )
        save_json(
            attempt_dir / "verified_script_acceptance.json",
            accepted["acceptance"],
        )
        report["script_sha256"] = accepted["acceptance"]["script_sha256"]

        voices_dir = attempt_dir / "voice_originals"
        voices_dir.mkdir()
        voice_paths = {}

        for speaker, reference in job["voices"].items():
            destination = voices_dir / Path(reference["key"]).name
            response = client.get_object(
                Bucket=bucket,
                Key=reference["key"],
            )
            with response["Body"] as source, destination.open("xb") as target:
                for chunk in iter(
                    lambda: source.read(1024 * 1024), b""
                ):
                    target.write(chunk)

            if file_sha256(destination) != reference["sha256"]:
                raise ValueError(f"Voice hash mismatch: {speaker}")
            voice_paths[speaker] = destination

        # A pre-existing claim always blocks a second generation.
        # An uncertain response also stops here without calling the renderer.
        put_json(
            client,
            bucket,
            f"runs/{run_id}/audio_submission_claim.json",
            {
                "run_id": run_id,
                "attempt_id": attempt_id,
                "status": "audio_submission_claimed",
                "script_sha256": report["script_sha256"],
                "voices": job["voices"],
                "automatic_retry_allowed": False,
            },
        )
        claimed = True

        result = render_episode(
            script=accepted["script"],
            expected_script_sha256=report["script_sha256"],
            voice_paths=voice_paths,
            output_dir=attempt_dir / "render",
        )
        report["status"] = "render_completed"
        report["duration_seconds"] = result["duration_seconds"]

    except Exception as error:
        report.update({
            "status": "failed",
            "error_type": type(error).__name__,
            "error": str(error),
        })
        raise

    finally:
        original_failure = sys.exc_info()[0] is not None
        report["finished_at"] = datetime.now(timezone.utc).isoformat()
        report["claim_confirmed"] = claimed
        save_json(attempt_dir / "job_result.json", report)
        print(json.dumps(report, indent=2), flush=True)
        print(f"Local evidence: {attempt_dir}", flush=True)

        # Do not upload another attempt when an existing/uncertain claim
        # blocked this job. Local evidence remains available.
        if claimed:
            upload_errors = []
            inventory = []

            for path in sorted(attempt_dir.rglob("*")):
                if not path.is_file():
                    continue
                relative = path.relative_to(attempt_dir).as_posix()
                try:
                    digest = file_sha256(path)
                    with path.open("rb") as stream:
                        client.put_object(
                            Bucket=bucket,
                            Key=prefix + relative,
                            Body=stream,
                            IfNoneMatch="*",
                        )
                    inventory.append({
                        "key": prefix + relative,
                        "sha256": digest,
                        "bytes": path.stat().st_size,
                    })
                except Exception as error:
                    upload_errors.append({
                        "file": relative,
                        "error_type": type(error).__name__,
                        "error": str(error),
                    })

            save_json(
                attempt_dir / "upload_result.json",
                {"uploaded": inventory, "errors": upload_errors},
            )

            if upload_errors:
                print(
                    json.dumps({"upload_errors": upload_errors}, indent=2),
                    flush=True,
                )
                if not original_failure:
                    raise RuntimeError(
                        "Audio upload incomplete; no success marker saved"
                    )

            elif not original_failure:
                # This is the authoritative completion marker.
                success = {
                    **report,
                    "status": "audio_completed",
                    "mp3_key": prefix + "render/episode.mp3",
                    "wav_key": prefix + "render/episode.wav",
                    "artifacts": inventory,
                }
                put_json(
                    client,
                    bucket,
                    prefix + "audio_success.json",
                    success,
                )
                save_json(attempt_dir / "audio_success.json", success)
                print(
                    f"Audio completed: s3://{bucket}/{success['mp3_key']}",
                    flush=True,
                )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--job-file", required=True)
    parser.add_argument("--profile")
    parser.add_argument("--work-root", default="local_runs/audio")
    args = parser.parse_args()

    with Path(args.job_file).open(encoding="utf-8") as stream:
        job = validate_job(json.load(stream))

    work_root = Path(args.work_root).resolve()
    work_root.mkdir(parents=True, exist_ok=True)

    # Prevent concurrent jobs on this machine when using the same work root.
    with (work_root / "audio_worker.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise RuntimeError("Another audio job is running") from error

        session = boto3.Session(
            profile_name=args.profile,
            region_name=job["region"],
        )
        client = session.client(
            "s3",
            config=Config(
                retries={"total_max_attempts": 1},
                connect_timeout=10,
                read_timeout=120,
            ),
        )
        execute_job(job=job, client=client, work_root=work_root)


if __name__ == "__main__":
    main()