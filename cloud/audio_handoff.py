"""Build and perform a verified job handoff without starting audio."""

import base64
import contextlib
import fcntl
import hashlib
import importlib.metadata
import io
import json
import os
from pathlib import Path
import pwd
import re
import shlex
import shutil
import sys
import tempfile
from uuid import UUID
from zipfile import ZipFile

INSTANCE_ID = "i-0acf87a780fb97103"
REGION = "us-east-2"
BUNDLE_SHA256 = (
    "1d33d19591873cbed775663ec297c8753a90"
    "ae07bba1d96ef646488a00c3c964"
)
APP_DIR = Path("/opt/mip/app")
WORK_ROOT = Path("/opt/mip/work")
BUNDLE_PATH = Path(f"/opt/mip/audio_worker_{BUNDLE_SHA256}.zip")
BOOT_PATH = Path("/proc/sys/kernel/random/boot_id")
WORKER_FILES = (
    "cloud/__init__.py",
    "cloud/acceptance.py",
    "cloud/output_validation.py",
    "cloud/run_context.py",
    "cloud/storage.py",
    "cloud/audio_renderer.py",
    "cloud/audio_job.py",
    "cloud/audio_requirements.txt",
)
EXPECTED_VERSIONS = {
    "torch": "2.6.0+cu124",
    "torchaudio": "2.6.0+cu124",
    "numpy": "1.26.4",
}


def require(condition, message):
    if not condition:
        raise ValueError(message)


def validate_handoff(payload):
    require(isinstance(payload, dict), "Expected a handoff object")
    require(set(payload) == {
        "run_id", "execution_arn", "instance_id", "owner_token", "lock_etag",
        "bucket", "region", "boot_id", "handoff_token",
        "job_sha256", "script_sha256",
    }, "Unexpected handoff fields")
    require(
        isinstance(payload["run_id"], str)
        and re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}-[0-9]{3}", payload["run_id"]),
        "Invalid handoff run_id",
    )
    prefix = (
        "arn:aws:states:us-east-2:340015815972:"
        "execution:mip-text-pipeline:"
    )
    arn = payload["execution_arn"]
    require(
        isinstance(arn, str) and arn.startswith(prefix)
        and bool(arn[len(prefix):].strip()),
        "Invalid handoff execution ARN",
    )
    require(payload["instance_id"] == INSTANCE_ID, "Wrong handoff instance")
    require(payload["region"] == REGION, "Wrong handoff region")
    for field in ("bucket", "owner_token", "lock_etag"):
        require(
            isinstance(payload[field], str) and bool(payload[field].strip()),
            f"Missing handoff {field}",
        )
    require(payload["lock_etag"].strip() != "*", "Expected a specific lock ETag")
    for field in ("handoff_token", "job_sha256", "script_sha256"):
        size = 32 if field == "handoff_token" else 64
        require(
            isinstance(payload[field], str)
            and re.fullmatch(rf"[0-9a-f]{{{size}}}", payload[field]),
            f"Invalid handoff {field}",
        )
    boot_id = payload["boot_id"]
    require(
        isinstance(boot_id, str) and str(UUID(boot_id)) == boot_id,
        "Invalid handoff boot UUID",
    )
    return payload


def verify_installed_worker():
    require(not APP_DIR.is_symlink(), "App directory is a symlink")
    require(not (APP_DIR / "cloud").is_symlink(), "Cloud directory is a symlink")
    require(not BUNDLE_PATH.is_symlink(), "Worker bundle is a symlink")
    require(
        hashlib.sha256(BUNDLE_PATH.read_bytes()).hexdigest() == BUNDLE_SHA256,
        "Installed worker bundle hash mismatch",
    )
    with ZipFile(BUNDLE_PATH) as archive:
        for name in WORKER_FILES:
            path = APP_DIR / name
            require(
                path.is_file() and not path.is_symlink(),
                f"Missing or linked worker file: {name}",
            )
            require(
                path.read_bytes() == archive.read(name),
                f"Installed worker file differs: {name}",
            )
    versions = {
        name: importlib.metadata.version(name)
        for name in EXPECTED_VERSIONS
    }
    require(versions == EXPECTED_VERSIONS, "Installed audio versions differ")
    require(shutil.which("ffmpeg") is not None, "FFmpeg is missing")
    return versions


def check_safeguards(source, token):
    output = io.StringIO()
    original_argv = sys.argv
    try:
        sys.argv = ["safeguard-probe", token]
        with contextlib.redirect_stdout(output):
            exec(compile(source, "<handoff-safeguards>", "exec"), {})
    finally:
        sys.argv = original_argv
    return json.loads(output.getvalue())


def verify_owner(client, payload):
    key = f"runs/audio-worker/{INSTANCE_ID}/ownership.json"
    response = client.get_object(
        Bucket=payload["bucket"], Key=key, IfMatch=payload["lock_etag"]
    )
    with response["Body"] as stream:
        ownership = json.load(stream)
    expected = {
        "schema_version": 1,
        "status": "ownership_held",
        **{key: payload[key] for key in (
            "run_id", "execution_arn", "instance_id", "owner_token"
        )},
    }
    require(
        ownership == expected and response.get("ETag") == payload["lock_etag"],
        "Handoff ownership no longer matches",
    )


def load_verified_job(client, payload):
    # Import installed worker code only after checking it against the bundle.
    from botocore.exceptions import ClientError
    from cloud.acceptance import json_sha256, load_accepted_script
    from cloud.audio_job import validate_job
    from cloud.storage import S3RunStorage

    storage = S3RunStorage(bucket=payload["bucket"], client=client)
    job = validate_job(storage.read_json(payload["run_id"], "audio_job.json"))
    require(
        job["run_id"] == payload["run_id"]
        and job["bucket"] == payload["bucket"]
        and job["region"] == payload["region"]
        and json_sha256(job) == payload["job_sha256"],
        "Handoff job identity or hash differs",
    )
    accepted = load_accepted_script(run_id=payload["run_id"], storage=storage)
    require(
        accepted["acceptance"]["script_sha256"] == payload["script_sha256"],
        "Handoff accepted script differs",
    )
    try:
        client.head_object(
            Bucket=payload["bucket"],
            Key=f'runs/{payload["run_id"]}/audio_submission_claim.json',
        )
    except ClientError as error:
        if error.response.get("ResponseMetadata", {}).get("HTTPStatusCode") != 404:
            raise
    else:
        raise ValueError("This run already has a permanent audio claim")
    return job


def handoff_job(payload, *, client, safeguard_source):
    validate_handoff(payload)
    require(
        BOOT_PATH.read_text().strip() == payload["boot_id"],
        "Worker boot changed before handoff",
    )
    lock_path = WORK_ROOT / "audio_worker.lock"
    require(
        lock_path.is_file() and not lock_path.is_symlink(),
        "Existing worker lock file is missing or linked",
    )
    with lock_path.open("r+") as worker_lock:
        fcntl.flock(worker_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        versions = verify_installed_worker()
        verify_owner(client, payload)
        job = load_verified_job(client, payload)
        probe = check_safeguards(safeguard_source, payload["handoff_token"])
        require(
            probe.get("status") == "audio_safeguards_verified"
            and probe.get("probe_token") == payload["handoff_token"]
            and probe.get("boot_id") == payload["boot_id"]
            and probe.get("generation_requested") is False,
            "Fresh handoff safeguards did not match",
        )
        verify_owner(client, payload)

        target = APP_DIR / "audio_job.json"
        require(not target.is_symlink(), "Job destination is a symlink")
        account = pwd.getpwnam("mip-audio")
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=".audio_job.", dir=APP_DIR
        )
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                os.fchmod(stream.fileno(), 0o600)
                os.fchown(stream.fileno(), account.pw_uid, account.pw_gid)
                json.dump(job, stream, sort_keys=True, allow_nan=False)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, target)
            require(json.loads(target.read_text()) == job, "Job read-back differs")
        finally:
            temporary.unlink(missing_ok=True)

    return {
        **payload,
        "status": "audio_job_handed_off",
        "bundle_sha256": BUNDLE_SHA256,
        "installed_versions": versions,
        "generation_requested": False,
    }


def build_audio_handoff_command(payload):
    from cloud.audio_service import AUDIO_SAFEGUARD_PROBE

    validate_handoff(payload)
    source = (
        "HANDOFF_SAFEGUARD_SOURCE = " + repr(AUDIO_SAFEGUARD_PROBE) + "\n"
        + Path(__file__).read_text(encoding="utf-8")
    )
    compile(source, "<audio-handoff>", "exec")
    encoded = base64.b64encode(
        json.dumps(payload, sort_keys=True, allow_nan=False).encode("utf-8")
    ).decode("ascii")
    return (
        "/opt/mip/venv/bin/python -B -c "
        + shlex.quote(source) + " " + shlex.quote(encoded)
    )


if __name__ == "__main__":
    import boto3
    from botocore.config import Config

    payload = validate_handoff(json.loads(
        base64.b64decode(sys.argv[1], validate=True)
    ))
    client = boto3.client(
        "s3",
        region_name=REGION,
        config=Config(
            retries={"total_max_attempts": 1},
            connect_timeout=10,
            read_timeout=30,
        ),
    )
    result = handoff_job(
        payload,
        client=client,
        safeguard_source=globals()["HANDOFF_SAFEGUARD_SOURCE"],
    )
    print(json.dumps(result, sort_keys=True, allow_nan=False), flush=True)