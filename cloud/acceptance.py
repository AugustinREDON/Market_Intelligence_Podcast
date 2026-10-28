"""Build automatic acceptance records without API calls or storage writes."""

import hashlib
import json

from cloud.output_validation import (
    validate_briefing_response,
    validate_script_for_acceptance,
)
from cloud.run_context import validate_run_context


ACCEPTANCE_POLICY_VERSION = "automatic_v1"


def text_sha256(text):
    """Hash exact text, including whitespace."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def json_sha256(value):
    """Hash JSON content independently of object-key order."""
    canonical = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return text_sha256(canonical)


def build_briefing_acceptance(*, run_context, validated_result):
    """Build a record from validate_completed_briefing's result.

    The caller must verify snapshot identity and saved artifacts before
    preserving this record.
    """
    run_context = validate_run_context(run_context)

    response_id = validated_result["response_id"]
    if not isinstance(response_id, str) or not response_id.strip():
        raise ValueError("Briefing acceptance requires a response ID")

    model = validated_result["model"]
    if not isinstance(model, str) or not model.strip():
        raise ValueError("Briefing acceptance requires a model")

    briefing = validate_briefing_response(
        json.dumps(
            validated_result["briefing"],
            ensure_ascii=False,
            allow_nan=False,
        ),
        run_context,
    )

    sources = validated_result["sources"]
    if not isinstance(sources, list) or not sources:
        raise ValueError("Briefing acceptance requires source evidence")

    for source in sources:
        if (
            not isinstance(source, dict)
            or not isinstance(source.get("url"), str)
            or not source["url"].strip()
        ):
            raise ValueError("Invalid briefing source evidence")

    return {
        "schema_version": 1,
        "policy_version": ACCEPTANCE_POLICY_VERSION,
        "acceptance_method": "automated_checks",
        "status": "accepted",
        "stage": "briefing",
        "run_id": run_context["run_id"],
        "response_id": response_id,
        "model": model,
        "run_context_sha256": json_sha256(run_context),
        "briefing_sha256": text_sha256(
            briefing["briefing_markdown"]
        ),
        "carry_forward_sha256": json_sha256(
            briefing["carry_forward"]
        ),
        "sources_sha256": json_sha256(sources),
        "source_count": len(sources),
        "eligible_uses": [
            "script_generation",
            "continuity",
        ],
    }

def build_script_acceptance(
    *,
    script_inputs,
    validated_result,
    briefing_acceptance,
):
    """Bind an accepted script to its verified briefing dependency.

    The caller must verify the saved briefing acceptance against its
    artifacts and verify the script snapshot identity before saving.
    """
    run_context = validate_run_context(
        script_inputs.get("run_context")
    )

    response_id = validated_result["response_id"]
    if not isinstance(response_id, str) or not response_id.strip():
        raise ValueError("Script acceptance requires a response ID")

    model = validated_result["model"]
    if (
        not isinstance(model, str)
        or not model.strip()
        or model != script_inputs.get("model")
    ):
        raise ValueError("Script model does not match saved inputs")

    if not isinstance(briefing_acceptance, dict):
        raise ValueError("Briefing acceptance must be an object")

    expected_fields = {
        "schema_version": 1,
        "policy_version": ACCEPTANCE_POLICY_VERSION,
        "acceptance_method": "automated_checks",
        "status": "accepted",
        "stage": "briefing",
        "run_id": run_context["run_id"],
        "run_context_sha256": json_sha256(run_context),
    }
    for field, expected in expected_fields.items():
        if briefing_acceptance.get(field) != expected:
            raise ValueError(
                f"Briefing acceptance dependency mismatch: {field}"
            )

    for input_field, acceptance_field in (
        ("briefing_response_id", "response_id"),
        ("briefing_sha256", "briefing_sha256"),
    ):
        value = script_inputs.get(input_field)
        if (
            not isinstance(value, str)
            or not value.strip()
            or value != briefing_acceptance.get(acceptance_field)
        ):
            raise ValueError(
                f"Script briefing dependency mismatch: {input_field}"
            )

    if "script_generation" not in briefing_acceptance.get(
        "eligible_uses", []
    ):
        raise ValueError("Briefing is not eligible for script generation")

    script = validate_script_for_acceptance(
        validated_result["script"]
    )

    return {
        "schema_version": 1,
        "policy_version": ACCEPTANCE_POLICY_VERSION,
        "acceptance_method": "automated_checks",
        "status": "accepted",
        "stage": "script",
        "run_id": run_context["run_id"],
        "response_id": response_id,
        "model": model,
        "run_context_sha256": json_sha256(run_context),
        "script_inputs_sha256": json_sha256(script_inputs),
        "script_sha256": text_sha256(script["script"]),
        "briefing_response_id": script_inputs["briefing_response_id"],
        "briefing_sha256": script_inputs["briefing_sha256"],
        "briefing_acceptance_sha256": json_sha256(
            briefing_acceptance
        ),
        "spoken_word_count": script["spoken_word_count"],
        "dialogue_line_count": script["dialogue_line_count"],
        "within_word_target": script["within_word_target"],
        "eligible_uses": ["audio_generation"],
    }