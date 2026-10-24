"""Run and inspect one researched briefing locally.

Start creates one paid generation.
Resume retrieves the existing response without generating again.
"""

import argparse
import json
import os
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

from cloud.prompt_loader import load_prompt_bundle
from cloud.text_generation import (
    build_briefing_request,
    create_client,
    retrieve_briefing,
    submit_briefing,
    validate_completed_briefing,
)


PROJECT_ROOT = Path(__file__).resolve().parent.parent

# Local generation outputs, excluded from Git.
RUNS_DIR = PROJECT_ROOT / "local_runs" / "text"


def write_json(path, value):
    """Replace a local JSON file only after writing its full contents."""
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    temporary.replace(path)


def save_response(run_dir, response):
    """Save the ID first, then the full response before validating."""
    write_json(
        run_dir / "response_id.json",
        {"response_id": response.id},
    )

    write_json(
        run_dir / "response.json",
        response.model_dump(mode="json"),
    )

    print("Response ID:", response.id)
    print("Status:", response.status)
    print("Saved evidence:", run_dir)


def preserve_output(path, content):
    """Create an output, or preserve it if its content already matches."""
    if path.exists():
        if path.read_text(encoding="utf-8") != content:
            raise ValueError(
                f"Existing output differs; refusing to overwrite: {path}"
            )
        return

    with path.open("x", encoding="utf-8") as file:
        file.write(content)


def inspect_response(run_dir, response, run_context):
    """Validate evidence and preserve existing validated outputs."""
    if not isinstance(response, dict):
        response = response.model_dump(mode="json")

    if response.get("status") in {"queued", "in_progress"}:
        print("Research is continuing. Use resume to check it later.")
        return

    try:
        result = validate_completed_briefing(response, run_context)
        briefing = result["briefing"]

        outputs = {
            "briefing.md": briefing["briefing_markdown"],
            "carry_forward.json": json.dumps(
                briefing["carry_forward"],
                ensure_ascii=False,
                indent=2,
            ),
            "research_evidence.json": json.dumps(
                {
                    "response_id": result["response_id"],
                    "model": result["model"],
                    "sources": result["sources"],
                    "usage": result["usage"],
                },
                ensure_ascii=False,
                indent=2,
            ),
        }

        # Check every existing output before creating any missing ones.
        for filename, content in outputs.items():
            path = run_dir / filename
            if path.exists():
                if path.read_text(encoding="utf-8") != content:
                    raise ValueError(
                        f"Existing output differs; refusing to overwrite: {path}"
                    )

        for filename, content in outputs.items():
            preserve_output(run_dir / filename, content)

    except ValueError as error:
        write_json(
            run_dir / "validation_error.json",
            {"error": str(error)},
        )
        raise

    print("Briefing structure validated.")
    print("Recorded sources:", len(result["sources"]))
    print("Usage:", json.dumps(result["usage"], indent=2))
    print("Read:", run_dir / "briefing.md")
    print("Editorial review is required before script generation.")


def start_run(args, client):
    # This first test deliberately uses no continuity input.
    if not args.no_prior_state:
        raise ValueError(
            "For this isolated first test, explicitly pass --no-prior-state"
        )

    now = datetime.now(ZoneInfo("America/Toronto")).replace(microsecond=0)

    run_context = {
        "episode_date": now.date().isoformat(),
        "timezone": "America/Toronto",
        "as_of_timestamp": now.isoformat(),
        "run_id": args.run_id,
        "prompt_version": "v1",
    }

    bundle = load_prompt_bundle(run_context["prompt_version"])

    request = build_briefing_request(
        model=os.environ["OPENAI_MODEL"],
        instructions=bundle["briefing"],
        run_context=run_context,
        prior_carry_forward=None,
    )

    # mkdir refuses an existing run, preventing accidental resubmission.
    run_dir = RUNS_DIR / run_context["run_id"]
    run_dir.mkdir(parents=True, exist_ok=False)

    write_json(
        run_dir / "inputs.json",
        {
            "run_context": run_context,
            "prior_carry_forward": None,
            "continuity_mode": "isolated_test_without_prior_state",
        },
    )
    write_json(run_dir / "request.json", request)

    # If submission fails, retain the directory and do not retry blindly.
    write_json(
        run_dir / "submission.json",
        {"status": "submission_started"},
    )

    try:
        response = submit_briefing(client, request)
    except Exception as error:
        write_json(
            run_dir / "submission.json",
            {
                "status": "submission_failed_or_acceptance_unknown",
                "error_type": type(error).__name__,
            },
        )
        print("Submission did not return a usable response.")
        print("Evidence:", run_dir)
        print("Do not automatically submit another generation.")
        raise

    save_response(run_dir, response)
    write_json(
        run_dir / "submission.json",
        {"status": "response_received", "response_id": response.id},
    )
    inspect_response(run_dir, response, run_context)


def resume_run(args, client):
    run_dir = RUNS_DIR / args.run_id

    inputs = json.loads(
        (run_dir / "inputs.json").read_text(encoding="utf-8")
    )
    identity = json.loads(
        (run_dir / "response_id.json").read_text(encoding="utf-8")
    )

    response = retrieve_briefing(client, identity["response_id"])
    save_response(run_dir, response)
    inspect_response(run_dir, response, inputs["run_context"])


def reprocess_run(args):
    """Validate saved evidence without creating an API client."""
    run_dir = RUNS_DIR / args.run_id

    inputs = json.loads(
        (run_dir / "inputs.json").read_text(encoding="utf-8")
    )
    identity = json.loads(
        (run_dir / "response_id.json").read_text(encoding="utf-8")
    )
    response = json.loads(
        (run_dir / "response.json").read_text(encoding="utf-8")
    )

    run_context = inputs["run_context"]

    if run_context["run_id"] != args.run_id:
        raise ValueError("Saved run ID does not match --run-id")

    if response.get("id") != identity["response_id"]:
        raise ValueError("Saved response ID does not match response_id.json")

    print("Reprocessing saved evidence offline:", run_dir)
    inspect_response(run_dir, response, run_context)



def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["start", "resume", "reprocess"])
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--no-prior-state", action="store_true")
    args = parser.parse_args()

    # Validate the ID before using it as part of a filesystem path.
    import re

    if not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}-[0-9]{3}", args.run_id):
        parser.error("--run-id must use YYYY-MM-DD-NNN")

    if args.action == "reprocess":
        reprocess_run(args)
        return

    load_dotenv(PROJECT_ROOT / ".env")
    client = create_client()

    if args.action == "start":
        start_run(args, client)
    else:
        resume_run(args, client)


if __name__ == "__main__":
    main()