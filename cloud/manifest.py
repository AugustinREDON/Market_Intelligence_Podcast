import json
import re
from datetime import date
from pathlib import Path


def validate_manifest(manifest):
    if not isinstance(manifest, dict):
        raise ValueError("Manifest must be a JSON object.")

    if type(manifest.get("schema_version")) is not int or manifest["schema_version"] != 1:
        raise ValueError("schema_version must be 1.")

    episode_date = manifest.get("episode_date")

    if not isinstance(episode_date, str):
        raise ValueError("episode_date must be a YYYY-MM-DD string.")

    try:
        parsed_date = date.fromisoformat(episode_date)
    except ValueError as error:
        raise ValueError("episode_date must be a valid YYYY-MM-DD date.") from error

    if parsed_date.isoformat() != episode_date:
        raise ValueError("episode_date must use YYYY-MM-DD format.")

    if manifest.get("timezone") != "America/Toronto":
        raise ValueError("timezone must be America/Toronto.")

    run_id = manifest.get("run_id")

    if not isinstance(run_id, str) or not re.fullmatch(
        rf"{re.escape(episode_date)}-[0-9]{{3}}", run_id
    ):
        raise ValueError("run_id must match episode_date followed by -NNN.")

    prompt_version = manifest.get("prompt_version")

    if not isinstance(prompt_version, str) or not re.fullmatch(
        r"v[1-9][0-9]*", prompt_version
    ):
        raise ValueError("prompt_version must look like v1, v2, etc.")

    expected_inputs = {
        "briefing_key": f"runs/{run_id}/briefing.md",
        "script_key": f"runs/{run_id}/script.md",
        "carry_forward_key": f"runs/{run_id}/carry_forward.json",
    }

    if manifest.get("inputs") != expected_inputs:
        raise ValueError("Input paths must match the run_id.")

    voices = manifest.get("voices")

    if not isinstance(voices, dict) or set(voices) != {"HOST_A", "HOST_B"}:
        raise ValueError("voices must contain exactly HOST_A and HOST_B.")

    for host, filename in (
        ("HOST_A", "host_a.wav"),
        ("HOST_B", "host_b.wav"),
    ):
        key = voices[host]

        if not isinstance(key, str) or not re.fullmatch(
            rf"assets/voices/v[1-9][0-9]*/{re.escape(filename)}", key
        ):
            raise ValueError(f"Invalid voice path for {host}.")

    expected_outputs = {
        "audio_key": f"published/{episode_date}/episode.mp3",
        "metadata_key": f"published/{episode_date}/episode.json",
    }

    if manifest.get("outputs") != expected_outputs:
        raise ValueError("Output paths must match the episode_date.")

    return manifest


def load_manifest(path):
    contents = Path(path).read_text(encoding="utf-8")
    return validate_manifest(json.loads(contents))