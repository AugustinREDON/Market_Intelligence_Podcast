import json

from cloud.run_context import validate_run_context


ALLOWED_TOPIC_STATUSES = {"ongoing", "developing", "upcoming"}


def _require_nonempty_string(value, field):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-empty string")


def _require_exact_keys(value, expected_keys, field):
    if not isinstance(value, dict):
        raise ValueError(f"{field} must be a JSON object")

    if set(value) != expected_keys:
        expected = ", ".join(sorted(expected_keys))
        raise ValueError(f"{field} must contain exactly: {expected}")


def _reject_duplicate_keys(pairs):
    result = {}

    for key, value in pairs:
        if key in result:
            raise ValueError(f"Duplicate JSON key: {key}")
        result[key] = value

    return result


def _reject_json_constant(value):
    raise ValueError(f"Invalid JSON constant: {value}")


def validate_briefing_response(raw_response, run_context):
    """Parse and validate generated briefing output.

    Returns the parsed response, or raises ValueError.
    """
    validate_run_context(run_context)
    _require_nonempty_string(raw_response, "Briefing response")

    try:
        response = json.loads(
            raw_response,
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_json_constant,
        )
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"Briefing response is not valid JSON: {exc.msg}"
        ) from exc

    _require_exact_keys(
        response,
        {"briefing_markdown", "carry_forward"},
        "Briefing response",
    )
    _require_nonempty_string(
        response["briefing_markdown"],
        "briefing_markdown",
    )

    carry_forward = response["carry_forward"]

    _require_exact_keys(
        carry_forward,
        {"episode_date", "as_of", "topics"},
        "carry_forward",
    )

    if carry_forward["episode_date"] != run_context["episode_date"]:
        raise ValueError(
            "carry_forward.episode_date must match the run context"
        )

    if carry_forward["as_of"] != run_context["as_of_timestamp"]:
        raise ValueError(
            "carry_forward.as_of must match the run context"
        )

    topics = carry_forward["topics"]

    if not isinstance(topics, list):
        raise ValueError("carry_forward.topics must be a list")

    for index, topic in enumerate(topics):
        field = f"carry_forward.topics[{index}]"

        _require_exact_keys(
            topic,
            {"topic", "status", "context"},
            field,
        )

        for key in ("topic", "status", "context"):
            _require_nonempty_string(topic[key], f"{field}.{key}")

        if topic["status"] not in ALLOWED_TOPIC_STATUSES:
            raise ValueError(
                f"{field}.status must be ongoing, developing, or upcoming"
            )

    return response