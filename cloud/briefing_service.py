"""Prepare briefing inputs in S3 without submitting a generation."""

import json
from uuid import uuid4
from cloud.prompt_loader import load_prompt_bundle
from cloud.run_context import validate_run_context
from cloud.storage import S3RunStorage
from cloud.acceptance import (
    build_briefing_acceptance,
    json_sha256,
    load_accepted_briefing,
)
from cloud.text_generation import (
    build_briefing_request,
    create_client,
    retrieve_briefing,
    submit_briefing,
    validate_completed_briefing,
)

def _load_prior_continuity(*, prior_run_id, run_context, storage):
    """Verify a selected prior run and describe its exact continuity."""
    run_context = validate_run_context(run_context)

    if not isinstance(prior_run_id, str) or not prior_run_id.strip():
        raise ValueError("An explicit prior_run_id is required")

    accepted = load_accepted_briefing(
        run_id=prior_run_id,
        storage=storage,
    )

    acceptance = accepted["acceptance"]

    if "continuity" not in acceptance["eligible_uses"]:
        raise ValueError("Prior briefing is not eligible for continuity")

    prior_context = accepted["run_context"]

    if prior_context["episode_date"] >= run_context["episode_date"]:
        raise ValueError(
            "Prior episode_date must be strictly before "
            "the new episode_date"
        )

    return {
        "prior_carry_forward": accepted["carry_forward"],
        "prior_state_provenance": {
            "run_id": prior_run_id,
            "episode_date": prior_context["episode_date"],
            "response_id": accepted["response_id"],
            "briefing_acceptance_sha256": json_sha256(acceptance),
            "carry_forward_sha256": acceptance["carry_forward_sha256"],
        },
    }

def prepare_briefing_run(
    *,
    run_context,
    model,
    prior_carry_forward=None,
    prior_run_id=None,
    storage=None,
):
    """Freeze a request using accepted continuity or no prior state."""
    run_context = validate_run_context(run_context)

    if prior_carry_forward is not None:
        raise ValueError(
            "Select an accepted prior_run_id instead of supplying "
            "unverified prior_carry_forward"
        )

    if storage is None:
        storage = S3RunStorage()

    continuity = None

    if prior_run_id is not None:
        continuity = _load_prior_continuity(
            prior_run_id=prior_run_id,
            run_context=run_context,
            storage=storage,
        )
        prior_carry_forward = continuity["prior_carry_forward"]

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
            "accepted_prior_state"
            if continuity is not None
            else "explicit_without_prior_state"
        ),
    }

    if continuity is not None:
        inputs["prior_state_provenance"] = (
            continuity["prior_state_provenance"]
        )

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

    mode = inputs.get("continuity_mode")

    required_fields = {
        "run_context",
        "model",
        "prior_carry_forward",
        "continuity_mode",
    }

    if mode == "accepted_prior_state":
        required_fields.add("prior_state_provenance")
    elif mode != "explicit_without_prior_state":
        raise ValueError(
            "Saved continuity_mode is unsupported; prior state "
            "requires accepted-run provenance"
        )

    if set(inputs) != required_fields:
        raise ValueError("Saved inputs have missing or unexpected fields")

    run_context = validate_run_context(inputs["run_context"])

    if run_context["run_id"] != run_id:
        raise ValueError("Saved run_id does not match the requested run")

    prior_state = inputs["prior_carry_forward"]

    if mode == "explicit_without_prior_state":
        if prior_state is not None:
            raise ValueError(
                "Saved continuity_mode does not match prior state"
            )
    else:
        provenance = inputs["prior_state_provenance"]

        if not isinstance(provenance, dict):
            raise ValueError("Saved prior_state_provenance must be an object")

        verified = _load_prior_continuity(
            prior_run_id=provenance.get("run_id"),
            run_context=run_context,
            storage=storage,
        )

        if provenance != verified["prior_state_provenance"]:
            raise ValueError(
                "Saved continuity provenance does not match "
                "the accepted prior run"
            )

        if prior_state != verified["prior_carry_forward"]:
            raise ValueError(
                "Saved prior carry-forward does not match "
                "the accepted prior run"
            )

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