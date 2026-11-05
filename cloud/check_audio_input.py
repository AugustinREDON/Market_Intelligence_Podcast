"""Verify an existing accepted script without generating text or audio."""

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

import boto3

from cloud.acceptance import load_accepted_script
from cloud.storage import S3RunStorage


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--bucket", required=True)
    parser.add_argument("--region", required=True)
    parser.add_argument("--profile", required=True)
    args = parser.parse_args()

    # Validate the run ID before using it in a local path.
    S3RunStorage._key(args.run_id, "script.md")

    output_dir = Path("local_runs") / "audio" / args.run_id
    output_dir.mkdir(parents=True, exist_ok=True)
    checked_at = datetime.now(timezone.utc)
    report_path = output_dir / (
        "audio_input_check_"
        + checked_at.strftime("%Y%m%dT%H%M%S%fZ")
        + ".json"
    )
    report = {
        "run_id": args.run_id,
        "bucket": args.bucket,
        "region": args.region,
        "checked_at": checked_at.isoformat(),
        "status": "failed",
        "generation_requested": False,
    }

    try:
        session = boto3.Session(
            profile_name=args.profile,
            region_name=args.region,
        )
        storage = S3RunStorage(
            bucket=args.bucket,
            client=session.client("s3"),
        )
        accepted = load_accepted_script(
            run_id=args.run_id,
            storage=storage,
        )
        acceptance = accepted["acceptance"]
        report.update({
            "status": "accepted_script_verified",
            "response_id": accepted["response_id"],
            "model": accepted["model"],
            "run_context": accepted["run_context"],
            "script_sha256": acceptance["script_sha256"],
            "spoken_word_count": acceptance["spoken_word_count"],
            "dialogue_line_count": acceptance["dialogue_line_count"],
            "within_word_target": acceptance["within_word_target"],
        })
    except Exception as error:
        report["error_type"] = type(error).__name__
        report["error"] = str(error)
        raise
    finally:
        with report_path.open("x", encoding="utf-8") as stream:
            json.dump(report, stream, indent=2, allow_nan=False)
            stream.write("\n")
        print(json.dumps(report, indent=2, allow_nan=False))
        print(f"Evidence saved: {report_path}")


if __name__ == "__main__":
    main()