"""Build an allowlisted audio worker ZIP without contacting AWS."""

import argparse
import hashlib
import io
import json
import re
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo


PROJECT_DIR = Path(__file__).resolve().parents[1]

WORKER_FILES = (
    "cloud/acceptance.py",
    "cloud/output_validation.py",
    "cloud/run_context.py",
    "cloud/storage.py",
    "cloud/audio_renderer.py",
    "cloud/audio_job.py",
    "cloud/audio_requirements.txt",
)


def sha256(content):
    return hashlib.sha256(content).hexdigest()


def preserve_bytes(path, content):
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

    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}-\d{3}", args.run_id):
        raise ValueError("run_id must use YYYY-MM-DD-NNN")

    payload = {}
    for name in WORKER_FILES:
        path = PROJECT_DIR / name
        if path.is_symlink():
            raise ValueError(f"Refusing symlink: {name}")
        content = path.read_bytes()
        if name.endswith(".py"):
            compile(content, name, "exec")
        payload[name] = content

    # Give the standalone deployment an explicit Python package.
    payload["cloud/__init__.py"] = b'"""MIP cloud audio worker."""\n'

    job_path = (
        PROJECT_DIR / "local_runs/audio" / args.run_id / "audio_job.json"
    )
    job_bytes = job_path.read_bytes()
    job = json.loads(job_bytes)
    if job.get("run_id") != args.run_id:
        raise ValueError("Audio job configuration has a different run_id")
    payload["audio_job.json"] = job_bytes

    manifest = {
        "schema_version": 1,
        "run_id": args.run_id,
        "files": {
            name: {"sha256": sha256(content), "bytes": len(content)}
            for name, content in sorted(payload.items())
        },
    }
    payload["audio_bundle_manifest.json"] = (
        json.dumps(manifest, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")

    buffer = io.BytesIO()
    with ZipFile(buffer, "w", compression=ZIP_DEFLATED) as archive:
        for name, content in sorted(payload.items()):
            info = ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = ZIP_DEFLATED
            info.external_attr = 0o100644 << 16
            archive.writestr(info, content)

    bundle = buffer.getvalue()
    with ZipFile(io.BytesIO(bundle)) as archive:
        if archive.testzip() is not None:
            raise ValueError("ZIP integrity check failed")

    digest = sha256(bundle)
    filename = f"audio_worker_{digest}.zip"
    relative_path = (
        Path("local_runs/audio") / args.run_id / "deployment" / filename
    )
    preserve_bytes(PROJECT_DIR / relative_path, bundle)

    evidence = {
        "run_id": args.run_id,
        "bundle_path": relative_path.as_posix(),
        "bucket": job["bucket"],
        "s3_key": f"runs/{args.run_id}/audio/deployment/{filename}",
        "bundle_sha256": digest,
        "bundle_bytes": len(bundle),
        "manifest": manifest,
    }
    preserve_bytes(
        (PROJECT_DIR / relative_path).with_suffix(".json"),
        (json.dumps(evidence, indent=2) + "\n").encode("utf-8"),
    )
    print(json.dumps(evidence, indent=2))


if __name__ == "__main__":
    main()