from cloud.prompt_loader import load_prompt_bundle
from cloud.run_context import validate_run_context


def lambda_handler(event, context):
    run_context = validate_run_context(event)
    bundle = load_prompt_bundle(run_context["prompt_version"])

    return {
        "mode": "mock",
        "status": "inputs_validated",
        "run_id": run_context["run_id"],
        "episode_date": run_context["episode_date"],
        "as_of_timestamp": run_context["as_of_timestamp"],
        "prompt_version": run_context["prompt_version"],
        "prompt_character_counts": {
            name: len(contents)
            for name, contents in bundle.items()
        },
    }