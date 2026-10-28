"""Prepare script requests from validated briefing artifacts.

Preparation reads/writes storage but does not contact OpenAI.
"""

import hashlib
import json
from uuid import uuid4
from cloud.acceptance import (
    build_briefing_acceptance,
    build_script_acceptance,
)
from cloud.output_validation import validate_briefing_response
from cloud.prompt_loader import load_prompt_bundle
from cloud.run_context import validate_run_context
from cloud.storage import S3RunStorage
from cloud.text_generation import (
    build_script_request,
    create_client,
    validate_completed_script,
)


def _load_accepted_briefing(*, run_id, storage=None):
    """Check saved briefing content and acceptance without loading prompts."""
    if storage is None:
        storage = S3RunStorage()

    inputs = storage.read_json(run_id, "inputs.json")
    reference = storage.read_json(
        run_id,
        "briefing_response_id.json",
    )
    validation = storage.read_json(
        run_id,
        "briefing_validation.json",
    )

    for name, value in (
        ("Briefing inputs", inputs),
        ("Briefing response reference", reference),
        ("Briefing validation marker", validation),
    ):
        if not isinstance(value, dict):
            raise ValueError(f"{name} must be an object")

    run_context = validate_run_context(inputs.get("run_context"))

    if run_context["run_id"] != run_id:
        raise ValueError("Briefing inputs have a different run_id")

    response_id = reference.get("response_id")

    if (
        reference.get("run_id") != run_id
        or not isinstance(response_id, str)
        or not response_id.strip()
    ):
        raise ValueError("Invalid briefing response reference")

    if (
        validation.get("status") != "validated"
        or validation.get("run_id") != run_id
        or validation.get("response_id") != response_id
    ):
        raise ValueError(
            "Briefing validation does not match the requested run"
        )

    source_count = validation.get("source_count")

    if (
        isinstance(source_count, bool)
        or not isinstance(source_count, int)
        or source_count <= 0
    ):
        raise ValueError("Briefing validation has no source evidence")

    briefing_markdown = storage.read_text(run_id, "briefing.md")
    carry_forward = storage.read_json(run_id, "carry_forward.json")

    # Recheck the extracted content against its original run context.
    validate_briefing_response(
        json.dumps(
            {
                "briefing_markdown": briefing_markdown,
                "carry_forward": carry_forward,
            },
            ensure_ascii=False,
            allow_nan=False,
        ),
        run_context,
    )

    # Require acceptance of the exact saved briefing and carry-forward.
    sources = storage.read_json(run_id, "briefing_sources.json")
    acceptance = storage.read_json(
        run_id, "briefing_acceptance.json"
    )

    expected_acceptance = build_briefing_acceptance(
        run_context=run_context,
        validated_result={
            "response_id": response_id,
            "model": inputs.get("model"),
            "briefing": {
                "briefing_markdown": briefing_markdown,
                "carry_forward": carry_forward,
            },
            "sources": sources,
        },
    )

    if source_count != expected_acceptance["source_count"]:
        raise ValueError(
            "Briefing source evidence does not match validation"
        )

    if acceptance != expected_acceptance:
        raise ValueError(
            "Briefing acceptance does not match the saved "
            "content, identity, or current acceptance policy"
        )

    return {
        "run_context": run_context,
        "response_id": response_id,
        "briefing_markdown": briefing_markdown,
        "acceptance": acceptance,
    }


def _build_script_artifacts(*, run_id, model, storage=None):
    """Build expected script inputs/request from an accepted briefing."""
    accepted = _load_accepted_briefing(
        run_id=run_id,
        storage=storage,
    )
    run_context = accepted["run_context"]
    response_id = accepted["response_id"]
    briefing_markdown = accepted["briefing_markdown"]


    bundle = load_prompt_bundle(run_context["prompt_version"])

    request = build_script_request(
        model=model,
        instructions=bundle["podcast"],
        run_context=run_context,
        briefing_markdown=briefing_markdown,
    )

    # Record which exact briefing supplied the script's market content.
    script_inputs = {
        "run_context": run_context,
        "model": model,
        "briefing_response_id": response_id,
        "briefing_sha256": hashlib.sha256(
            briefing_markdown.encode("utf-8")
        ).hexdigest(),
    }

    return {
        "run_context": run_context,
        "inputs": script_inputs,
        "request": request,
    }


def prepare_script_run(*, run_id, model, storage=None):
    """Preserve a script request without contacting OpenAI."""
    if storage is None:
        storage = S3RunStorage()

    prepared = _build_script_artifacts(
        run_id=run_id,
        model=model,
        storage=storage,
    )

    artifacts = {}
    artifacts["script_inputs.json"] = storage.preserve_json(
        run_id,
        "script_inputs.json",
        prepared["inputs"],
    )
    artifacts["script_request.json"] = storage.preserve_json(
        run_id,
        "script_request.json",
        prepared["request"],
    )

    return {
        "mode": "script_preparation",
        "status": "script_inputs_and_request_preserved",
        "run_id": run_id,
        "briefing_response_id": (
            prepared["inputs"]["briefing_response_id"]
        ),
        "artifacts": artifacts,
        "generation_requested": False,
    }


def load_prepared_script_run(*, run_id, storage=None):
    """Check frozen artifacts and return the original saved request."""
    if storage is None:
        storage = S3RunStorage()

    inputs = storage.read_json(run_id, "script_inputs.json")
    request = storage.read_json(run_id, "script_request.json")

    if not isinstance(inputs, dict):
        raise ValueError("Saved script inputs must be an object")

    if not isinstance(request, dict):
        raise ValueError("Saved script request must be an object")

    expected = _build_script_artifacts(
        run_id=run_id,
        model=inputs.get("model"),
        storage=storage,
    )

    if inputs != expected["inputs"]:
        raise ValueError(
            "Saved script inputs do not match the validated briefing"
        )

    if request != expected["request"]:
        raise ValueError(
            "Saved script request does not match the inputs and "
            "current prompt/configuration; review before submission"
        )

    return {
        "run_context": expected["run_context"],
        "request": request,
    }

def submit_prepared_script_run(
    *,
    run_id,
    storage=None,
    client_factory=None,
):
    """Submit a checked script request once and preserve its response."""
    if storage is None:
        storage = S3RunStorage()

    prepared = load_prepared_script_run(
        run_id=run_id,
        storage=storage,
    )

    if client_factory is None:
        client_factory = lambda: create_client(timeout=30.0)

    # Initialize the client before claiming submission.
    with client_factory() as client:
        claimed = storage.claim_script_submission(run_id)

        if claimed is not True:
            return {
                "mode": "script_submission",
                "status": "existing_claim_submission_blocked",
                "run_id": run_id,
                "generation_requested": False,
            }

        stage = "openai_submission"
        response_id = None

        try:
            # Send the original saved request, with no retry loop.
            response = client.responses.create(**prepared["request"])

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
                "script_response_id.json",
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

            if not isinstance(initial_response, dict):
                raise ValueError("Initial response must be an object")

            storage.preserve_json(
                run_id,
                "script_response_initial.json",
                initial_response,
            )

        except Exception as error:
            # Keep the claim: OpenAI acceptance may be uncertain.
            evidence = {
                "run_id": run_id,
                "stage": stage,
                "error_type": type(error).__name__,
                "response_id": response_id,
                "automatic_resubmission_allowed": False,
            }

            print(
                f"[script_submission] run_id={run_id} "
                f"stage={stage} response_id={response_id} "
                f"error_type={type(error).__name__}",
                flush=True,
            )

            try:
                storage.preserve_json(
                    run_id,
                    "script_submission_failure.json",
                    evidence,
                )
            except Exception as evidence_error:
                print(
                    "[script_submission] Failure evidence could not "
                    f"be saved: {type(evidence_error).__name__}",
                    flush=True,
                )

            raise

    return {
        "mode": "script_submission",
        "status": "initial_response_preserved",
        "run_id": run_id,
        "response_id": response_id,
        "response_status": initial_response.get("status"),
        "generation_requested": True,
    }

def retrieve_script_run(
    *,
    run_id,
    storage=None,
    client_factory=None,
):
    """Retrieve an existing script response and preserve a fresh snapshot."""
    if storage is None:
        storage = S3RunStorage()

    reference = storage.read_json(run_id, "script_response_id.json")

    if not isinstance(reference, dict):
        raise ValueError("Saved script response reference must be an object")

    response_id = reference.get("response_id")

    if (
        reference.get("run_id") != run_id
        or not isinstance(response_id, str)
        or not response_id.strip()
    ):
        raise ValueError("Invalid saved script response reference")

    if client_factory is None:
        client_factory = lambda: create_client(timeout=30.0)

    with client_factory() as client:
        response = client.responses.retrieve(response_id)

    snapshot = (
        response
        if isinstance(response, dict)
        else response.model_dump(mode="json")
    )

    if not isinstance(snapshot, dict):
        raise ValueError("Retrieved script response must be an object")

    filename = f"script_response_snapshot_{uuid4().hex}.json"
    storage.preserve_json(run_id, filename, snapshot)

    # Preserve raw evidence before checking identity and status.
    if snapshot.get("id") != response_id:
        raise ValueError("Retrieved script response ID does not match saved ID")

    response_status = snapshot.get("status")

    if response_status in {"queued", "in_progress"}:
        status = "generation_pending"
    elif response_status == "completed":
        status = "completed_response_preserved"
    elif response_status in {"failed", "cancelled", "incomplete"}:
        status = "generation_unsuccessful"
    else:
        raise ValueError(
            f"Unexpected script response status: {response_status}"
        )

    return {
        "mode": "script_retrieval",
        "status": status,
        "run_id": run_id,
        "response_id": response_id,
        "response_status": response_status,
        "snapshot_filename": filename,
        "generation_requested": False,
    }

def validate_saved_script_run(
    *,
    run_id,
    snapshot_filename,
    storage=None,
):
    """Validate a saved script snapshot and extract artifacts offline."""
    if storage is None:
        storage = S3RunStorage()

    if (
        not isinstance(snapshot_filename, str)
        or not snapshot_filename.startswith("script_response_snapshot_")
        or not snapshot_filename.endswith(".json")
    ):
        raise ValueError("An explicit script snapshot filename is required")

    inputs = storage.read_json(run_id, "script_inputs.json")

    if not isinstance(inputs, dict):
        raise ValueError("Saved script inputs must be an object")

    run_context = validate_run_context(inputs.get("run_context"))

    if run_context["run_id"] != run_id:
        raise ValueError("Saved script inputs have a different run_id")

    model = inputs.get("model")
    if not isinstance(model, str) or not model.strip():
        raise ValueError("Saved script inputs have no usable model")

    reference = storage.read_json(run_id, "script_response_id.json")

    if not isinstance(reference, dict):
        raise ValueError("Saved script response reference must be an object")

    response_id = reference.get("response_id")

    if (
        reference.get("run_id") != run_id
        or not isinstance(response_id, str)
        or not response_id.strip()
    ):
        raise ValueError("Invalid saved script response reference")

    snapshot = storage.read_json(run_id, snapshot_filename)

    if not isinstance(snapshot, dict):
        raise ValueError("Saved script snapshot must be an object")

    if snapshot.get("id") != response_id:
        raise ValueError("Script snapshot response ID does not match saved ID")

    if snapshot.get("model") != model:
        raise ValueError("Script snapshot model does not match saved inputs")

    # Complete validation before writing any extracted artifacts.
    result = validate_completed_script(snapshot)

    # Recheck the briefing dependency without loading current prompts.
    accepted_briefing = _load_accepted_briefing(
        run_id=run_id,
        storage=storage,
    )

    acceptance = build_script_acceptance(
        script_inputs=inputs,
        validated_result=result,
        briefing_acceptance=accepted_briefing["acceptance"],
    )

    artifacts = {}
    artifacts["script.md"] = storage.preserve_text(
        run_id,
        "script.md",
        result["script"],
        content_type="text/markdown; charset=utf-8",
    )
    artifacts["script_usage.json"] = storage.preserve_json(
        run_id,
        "script_usage.json",
        {
            "response_id": response_id,
            "model": result["model"],
            "usage": result["usage"],
        },
    )

    # Preserve the original validation evidence before acceptance.
    artifacts["script_validation.json"] = storage.preserve_json(
        run_id,
        "script_validation.json",
        {
            "run_id": run_id,
            "response_id": response_id,
            "model": result["model"],
            "status": "validated",
            "script_sha256": hashlib.sha256(
                result["script"].encode("utf-8")
            ).hexdigest(),
            "spoken_word_count": result["spoken_word_count"],
            "dialogue_line_count": result["dialogue_line_count"],
            "within_word_target": result["within_word_target"],
            "acceptance_status": "requires_editorial_review",
            "audio_generation_allowed": False,
        },
    )

        # Acceptance is saved last, after successful artifact preservation.
    artifacts["script_acceptance.json"] = storage.preserve_json(
        run_id,
        "script_acceptance.json",
        acceptance,
    )

    return {
        "mode": "script_validation",
        "status": "script_validated_and_preserved",
        "run_id": run_id,
        "response_id": response_id,
        "snapshot_filename": snapshot_filename,
        "spoken_word_count": result["spoken_word_count"],
        "dialogue_line_count": result["dialogue_line_count"],
        "within_word_target": result["within_word_target"],
        "acceptance_status": "accepted",
        "audio_generation_allowed": True,
        "artifacts": artifacts,
        "generation_requested": False,
    }