def lambda_handler(event, context):
    prompt = event.get("prompt", "Write a podcast introduction.")

    return {
        "mode": "mock",
        "received_prompt": prompt,
        "script": (
            "HOST_A: Welcome to your daily market briefing.\n"
            "HOST_B: Let's explore today's major stories."
        ),
    }