import json

from lambda_function import lambda_handler


if __name__ == "__main__":
    test_event = {
        "prompt": "Write an introduction for my market intelligence podcast."
    }

    result = lambda_handler(test_event, context=None)
    print(json.dumps(result, indent=2))