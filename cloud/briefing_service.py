"""Prepare briefing inputs in S3 without submitting a generation."""

from cloud.prompt_loader import load_prompt_bundle
from cloud.run_context import validate_run_context
from cloud.storage import S3RunStorage
from cloud.text_generation import build_briefing_request


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