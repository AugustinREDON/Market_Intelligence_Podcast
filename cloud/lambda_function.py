import os
from cloud.prompt_loader import load_prompt_bundle
from cloud.run_context import validate_run_context
from time import perf_counter


def lambda_handler(event, context):

    if event.get("action") == "check_s3_storage":
        import json
        from uuid import uuid4

        import boto3

        bucket = os.environ.get("MIP_S3_BUCKET")
        if not bucket:
            raise ValueError(
                "MIP_S3_BUCKET must be configured for this check"
            )

        key = f"runs/storage-check/{uuid4().hex}.json"
        payload = {
            "purpose": "Lambda S3 write/read check",
            "generation_requested": False,
        }
        body = json.dumps(payload).encode("utf-8")

        s3 = boto3.client("s3")
        s3.put_object(
            Bucket=bucket,
            Key=key,
            Body=body,
            ContentType="application/json",
        )

        response = s3.get_object(Bucket=bucket, Key=key)
        with response["Body"] as stream:
            saved_body = stream.read()

        if saved_body != body:
            raise ValueError("S3 read-back did not match uploaded content")

        return {
            "mode": "storage_check",
            "status": "write_read_verified",
            "bucket": bucket,
            "key": key,
            "generation_requested": False,
        }

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