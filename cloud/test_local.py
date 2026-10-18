import json

from cloud.output_validation import (
    validate_briefing_response,
    validate_script_response,
)
from cloud.prompt_loader import load_prompt_bundle
from cloud.run_context import validate_run_context


RUN_CONTEXT = {
    "episode_date": "2026-10-16",
    "timezone": "America/Toronto",
    "as_of_timestamp": "2026-10-16T06:00:00-04:00",
    "run_id": "2026-10-16-001",
    "prompt_version": "v1",
}


def mock_generate_briefing(instructions, run_context):
    """Stand in for the future briefing API call."""
    if not instructions.strip():
        raise ValueError("Briefing instructions are missing")

    return json.dumps({
        "briefing_markdown": (
            "# Sample Market Briefing\n\n"
            "This is fictional test content, not current market news. "
            "Investors are awaiting upcoming company earnings."
        ),
        "carry_forward": {
            "episode_date": run_context["episode_date"],
            "as_of": run_context["as_of_timestamp"],
            "topics": [
                {
                    "topic": "Company earnings",
                    "status": "upcoming",
                    "context": "Fictional topic for testing continuity.",
                }
            ],
        },
    })


def mock_generate_script(instructions, run_context, briefing):
    """Stand in for the future podcast API call."""
    if not instructions.strip():
        raise ValueError("Podcast instructions are missing")

    if not briefing.strip():
        raise ValueError("The script stage did not receive a briefing")

    # Include the received briefing to make the handoff observable.
    # A real API call will turn this material into natural dialogue.
    briefing_text = " ".join(briefing.splitlines())

    return (
        f"HOST_A: This is a test episode for "
        f"{run_context['episode_date']}.\n"
        f"HOST_B: The supplied briefing reads: {briefing_text}\n"
        "HOST_A: That completes this local workflow test."
    )


def main():
    run_context = validate_run_context(RUN_CONTEXT)
    bundle = load_prompt_bundle(run_context["prompt_version"])
    print("1. Run context validated and prompts loaded.")

    raw_briefing = mock_generate_briefing(
        instructions=bundle["briefing"],
        run_context=run_context,
    )

    # If validation raises an error, execution stops here.
    briefing_result = validate_briefing_response(
        raw_briefing,
        run_context,
    )
    print("2. Sample briefing and carry-forward validated.")

    raw_script = mock_generate_script(
        instructions=bundle["podcast"],
        run_context=run_context,
        briefing=briefing_result["briefing_markdown"],
    )

    script_result = validate_script_response(raw_script)
    print("3. Briefing passed to script stage; script structure validated.")

    print(json.dumps({
        "mode": "mock",
        "status": "local_flow_validated",
        "run_id": run_context["run_id"],
        "carry_forward_topic_count": len(
            briefing_result["carry_forward"]["topics"]
        ),
        "spoken_word_count": script_result["spoken_word_count"],
        "within_word_target": script_result["within_word_target"],
    }, indent=2))


if __name__ == "__main__":
    main()