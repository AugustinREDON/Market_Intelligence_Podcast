import json

from cloud.lambda_function import lambda_handler


if __name__ == "__main__":
    test_event = {
        "episode_date": "2026-10-16",
        "timezone": "America/Toronto",
        "as_of_timestamp": "2026-10-16T06:00:00-04:00",
        "run_id": "2026-10-16-001",
        "prompt_version": "v1",
    }

    result = lambda_handler(test_event, context=None)
    print(json.dumps(result, indent=2))