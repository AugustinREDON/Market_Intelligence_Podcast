"""Prepare briefing inputs in S3 without submitting a generation."""

import json
from uuid import uuid4
from cloud.prompt_loader import load_prompt_bundle
from cloud.run_context import validate_run_context
from cloud.storage import S3RunStorage
from cloud.acceptance import build_briefing_acceptance
from cloud.text_generation import (
    build_briefing_request,
    create_client,
    retrieve_briefing,
    submit_briefing,
    validate_completed_briefing,
)


def prepare_briefing_run(
    *,
    run_context,
    model,
    prior_carry_forward=None,
):
    """Preserve inputs and the complete request for one run."""
    run_context = validate_run_context(run_context)
    bundle = load_prompt_bundle(run_context["prompt_version"])

    request = build_briefing_request(
        model=model,
        instructions=bundle["briefing"],
        run_context=run_context,
        prior_carry_forward=prior_carry_forward,
    )

    inputs = {
        "run_context": run_context,
        "model": model,
        "prior_carry_forward": prior_carry_forward,
        "continuity_mode": (
            "supplied_prior_state"
            if prior_carry_forward is not None
            else "explicit_without_prior_state"
        ),
    }

    storage = S3RunStorage()
    run_id = run_context["run_id"]

    inputs_status = storage.preserve_json(
        run_id,
        "inputs.json",
        inputs,
    )
    request_status = storage.preserve_json(
        run_id,
        "request.json",
        request,
    )

    return {
        "mode": "briefing_preparation",
        "status": "inputs_and_request_preserved",
        "run_id": run_id,
        "artifacts": {
            "inputs.json": inputs_status,
            "request.json": request_status,
        },
        "generation_requested": False,
    }

def load_prepared_briefing_run(*, run_id, storage=None):
    """Read and check frozen artifacts without submitting a generation."""
    if storage is None:
        storage = S3RunStorage()

    inputs = storage.read_json(run_id, "inputs.json")
    request = storage.read_json(run_id, "request.json")

    if not isinstance(inputs, dict):
        raise ValueError("Saved inputs must be a JSON object")

    if not isinstance(request, dict):
        raise ValueError("Saved request must be a JSON object")

    required_fields = {
        "run_context",
        "model",
        "prior_carry_forward",
        "continuity_mode",
    }
    if set(inputs) != required_fields:
        raise ValueError("Saved inputs have missing or unexpected fields")

    run_context = validate_run_context(inputs["run_context"])

    if run_context["run_id"] != run_id:
        raise ValueError("Saved run_id does not match the requested run")

    prior_state = inputs["prior_carry_forward"]
    expected_mode = (
        "supplied_prior_state"
        if prior_state is not None
        else "explicit_without_prior_state"
    )
    if inputs["continuity_mode"] != expected_mode:
        raise ValueError("Saved continuity_mode does not match prior state")

    bundle = load_prompt_bundle(run_context["prompt_version"])

    expected_request = build_briefing_request(
        model=inputs["model"],
        instructions=bundle["briefing"],
        run_context=run_context,
        prior_carry_forward=prior_state,
    )

    # JSON key order can change when inputs are saved and loaded.
    # Compare embedded inputs by value, preserving the saved request.
    input_prefix = "Generate this edition using the supplied inputs:\n"
    saved_input = request.get("input")

    if (
        not isinstance(saved_input, str)
        or not saved_input.startswith(input_prefix)
    ):
        raise ValueError("Saved request input has an unexpected format")

    try:
        saved_payload = json.loads(saved_input[len(input_prefix):])
        expected_payload = json.loads(
            expected_request["input"][len(input_prefix):]
        )
    except ValueError:
        raise ValueError("Saved request input contains invalid JSON") from None

    if saved_payload != expected_payload:
        raise ValueError("Saved request input does not match saved inputs")

    # All other request fields still require an exact match.
    expected_request["input"] = saved_input

    if request != expected_request:
        raise ValueError(
            "Saved request does not match the inputs and current "
            "prompt/configuration; review before submission"
        )

    return {
        "run_context": run_context,
        "request": request,
    }

def submit_prepared_briefing_run(
    *,
    run_id,
    storage=None,
    client_factory=None,
):
    """Submit a prepared run once and preserve its initial response."""
    if storage is None:
        storage = S3RunStorage()

    prepared = load_prepared_briefing_run(
        run_id=run_id,
        storage=storage,
    )

    if client_factory is None:
        client_factory = lambda: create_client(timeout=30.0)

    # Initialize dependencies before claiming permission to submit.
    with client_factory() as client:
        claimed = storage.claim_briefing_submission(run_id)

        if not claimed:
            return {
                "mode": "briefing_submission",
                "status": "existing_claim_submission_blocked",
                "run_id": run_id,
                "generation_requested": False,
            }

        stage = "openai_submission"
        response_id = None

        try:
            response = submit_briefing(client, prepared["request"])

            response_id = (
                response.get("id")
                if isinstance(response, dict)
                else getattr(response, "id", None)
            )

            if not isinstance(response_id, str) or not response_id.strip():
                raise ValueError("OpenAI response has no usable response ID")

            stage = "response_id_storage"
            storage.preserve_json(
                run_id,
                "briefing_response_id.json",
                {
                    "run_id": run_id,
                    "response_id": response_id,
                },
            )

            stage = "initial_response_storage"
            initial_response = (
                response
                if isinstance(response, dict)
                else response.model_dump(mode="json")
            )
            storage.preserve_json(
                run_id,
                "briefing_response_initial.json",
                initial_response,
            )

        except Exception as error:
            # Acceptance may be uncertain. Keep the claim in every case.
            evidence = {
                "run_id": run_id,
                "stage": stage,
                "error_type": type(error).__name__,
                "response_id": response_id,
                "automatic_resubmission_allowed": False,
            }

            # Log recovery information even if S3 is unavailable.
            print(
                f"[briefing_submission] run_id={run_id} "
                f"stage={stage} response_id={response_id} "
                f"error_type={type(error).__name__}",
                flush=True,
            )

            try:
                storage.preserve_json(
                    run_id,
                    "briefing_submission_failure.json",
                    evidence,
                )
            except Exception as evidence_error:
                print(
                    "[briefing_submission] Failure evidence could not "
                    f"be saved: {type(evidence_error).__name__}",
                    flush=True,
                )

            raise

    return {
        "mode": "briefing_submission",
        "status": "initial_response_preserved",
        "run_id": run_id,
        "response_id": response_id,
        "response_status": initial_response.get("status"),
        "generation_requested": True,
    }

def retrieve_briefing_run(
    *,
    run_id,
    storage=None,
    client_factory=None,
):
    """Retrieve an existing generation and preserve a fresh snapshot."""
    if storage is None:
        storage = S3RunStorage()

    reference = storage.read_json(run_id, "briefing_response_id.json")

    if not isinstance(reference, dict):
        raise ValueError("Saved response reference must be an object")

    if reference.get("run_id") != run_id:
        raise ValueError("Saved response reference has a different run_id")

    response_id = reference.get("response_id")
    if not isinstance(response_id, str) or not response_id.strip():
        raise ValueError("Saved response reference has no usable response ID")

    if client_factory is None:
        client_factory = lambda: create_client(timeout=30.0)

    with client_factory() as client:
        response = retrieve_briefing(client, response_id)

    snapshot = (
        response
        if isinstance(response, dict)
        else response.model_dump(mode="json")
    )

    if not isinstance(snapshot, dict):
        raise ValueError("Retrieved response must be a JSON object")

    # Each retrieval gets its own immutable artifact.
    filename = f"briefing_response_snapshot_{uuid4().hex}.json"
    storage.preserve_json(run_id, filename, snapshot)

    # Preserve the evidence before checking the returned response.
    if snapshot.get("id") != response_id:
        raise ValueError("Retrieved response ID does not match saved ID")

    response_status = snapshot.get("status")
    if response_status in {"queued", "in_progress"}:
        status = "generation_pending"
    elif response_status == "completed":
        status = "completed_response_preserved"
    elif response_status in {"failed", "cancelled", "incomplete"}:
        status = "generation_unsuccessful"
    else:
        raise ValueError(
            f"Unexpected response status: {response_status}"
        )

    return {
        "mode": "briefing_retrieval",
        "status": status,
        "run_id": run_id,
        "response_id": response_id,
        "response_status": response_status,
        "snapshot_filename": filename,
        "generation_requested": False,
    }

def validate_saved_briefing_run(
    *,
    run_id,
    snapshot_filename,
    storage=None,
):
    """Validate a saved response and extract artifacts without API calls."""
    if storage is None:
        storage = S3RunStorage()

    if (
        not isinstance(snapshot_filename, str)
        or not snapshot_filename.startswith("briefing_response_snapshot_")
        or not snapshot_filename.endswith(".json")
    ):
        raise ValueError("An explicit response snapshot filename is required")

    inputs = storage.read_json(run_id, "inputs.json")
    if not isinstance(inputs, dict):
        raise ValueError("Saved inputs must be an object")

    run_context = validate_run_context(inputs["run_context"])
    if run_context["run_id"] != run_id:
        raise ValueError("Saved inputs have a different run_id")

    reference = storage.read_json(run_id, "briefing_response_id.json")
    if not isinstance(reference, dict):
        raise ValueError("Saved response reference must be an object")

    response_id = reference.get("response_id")
    if (
        reference.get("run_id") != run_id
        or not isinstance(response_id, str)
        or not response_id.strip()
    ):
        raise ValueError("Invalid saved response reference")

    snapshot = storage.read_json(run_id, snapshot_filename)
    if not isinstance(snapshot, dict):
        raise ValueError("Saved snapshot must be an object")

    if snapshot.get("id") != response_id:
        raise ValueError("Snapshot response ID does not match saved ID")

    if snapshot.get("model") != inputs["model"]:
        raise ValueError("Snapshot model does not match saved inputs")

    # Validate everything before writing extracted artifacts.
    result = validate_completed_briefing(snapshot, run_context)
    briefing = result["briefing"]

    acceptance = build_briefing_acceptance(
    run_context=run_context,
    validated_result=result,
    )

    artifacts = {}
    artifacts["briefing.md"] = storage.preserve_text(
        run_id,
        "briefing.md",
        briefing["briefing_markdown"],
        content_type="text/markdown; charset=utf-8",
    )
    artifacts["carry_forward.json"] = storage.preserve_json(
        run_id,
        "carry_forward.json",
        briefing["carry_forward"],
    )
    artifacts["briefing_sources.json"] = storage.preserve_json(
        run_id,
        "briefing_sources.json",
        result["sources"],
    )
    artifacts["briefing_usage.json"] = storage.preserve_json(
        run_id,
        "briefing_usage.json",
        {
            "response_id": response_id,
            "model": result["model"],
            "usage": result["usage"],
        },
    )

    # Write this last so it marks successful extraction of all artifacts.
    artifacts["briefing_validation.json"] = storage.preserve_json(
        run_id,
        "briefing_validation.json",
        {
            "run_id": run_id,
            "response_id": response_id,
            "status": "validated",
            "source_count": len(result["sources"]),
        },
    )

    # Save acceptance only after all extracted artifacts and the
    # existing validation marker have been preserved successfully.
    artifacts["briefing_acceptance.json"] = storage.preserve_json(
        run_id,
        "briefing_acceptance.json",
        acceptance,
    )

    return {
        "mode": "briefing_validation",
        "status": "briefing_validated_and_preserved",
        "run_id": run_id,
        "response_id": response_id,
        "snapshot_filename": snapshot_filename,
        "source_count": len(result["sources"]),
        "artifacts": artifacts,
        "generation_requested": False,
    }