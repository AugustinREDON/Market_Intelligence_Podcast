import os
from cloud.prompt_loader import load_prompt_bundle
from cloud.run_context import validate_run_context
from time import perf_counter


def lambda_handler(event, context):

    if event.get("action") == "check_openai_secret":
        started = perf_counter()

        def log_stage(message):
            elapsed = perf_counter() - started
            print(f"[secret_check] {elapsed:.2f}s: {message}", flush=True)

        if not os.environ.get("OPENAI_SECRET_ID"):
            raise ValueError(
                "OPENAI_SECRET_ID must be configured for this check"
            )

        log_stage("Importing text_generation")
        from cloud.text_generation import create_client
        log_stage("text_generation imported")

        with create_client():
            log_stage("Client initialized successfully")
            return {
                "mode": "configuration_check",
                "status": "secret_loaded_client_initialized",
                "generation_requested": False,
            }

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