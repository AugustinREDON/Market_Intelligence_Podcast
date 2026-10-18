import re
from datetime import date, datetime
from zoneinfo import ZoneInfo


TIMEZONE = "America/Toronto"


def validate_run_context(context):
    if not isinstance(context, dict):
        raise ValueError("Run context must be a JSON object.")

    episode_date = context.get("episode_date")

    if not isinstance(episode_date, str):
        raise ValueError("episode_date must be a YYYY-MM-DD string.")

    try:
        parsed_date = date.fromisoformat(episode_date)
    except ValueError as error:
        raise ValueError("episode_date must be a valid date.") from error

    if parsed_date.isoformat() != episode_date:
        raise ValueError("episode_date must use YYYY-MM-DD format.")

    if context.get("timezone") != TIMEZONE:
        raise ValueError(f"timezone must be {TIMEZONE}.")

    run_id = context.get("run_id")

    if not isinstance(run_id, str) or not re.fullmatch(
        rf"{re.escape(episode_date)}-[0-9]{{3}}", run_id
    ):
        raise ValueError("run_id must match episode_date followed by -NNN.")

    version = context.get("prompt_version")

    if not isinstance(version, str) or not re.fullmatch(
        r"v[1-9][0-9]*", version
    ):
        raise ValueError("prompt_version must look like v1, v2, etc.")

    timestamp = context.get("as_of_timestamp")

    if not isinstance(timestamp, str) or not re.fullmatch(
        r"[0-9]{4}-[0-9]{2}-[0-9]{2}T"
        r"[0-9]{2}:[0-9]{2}:[0-9]{2}"
        r"[+-][0-9]{2}:[0-9]{2}",
        timestamp,
    ):
        raise ValueError(
            "as_of_timestamp must use YYYY-MM-DDTHH:MM:SS±HH:MM."
        )

    try:
        cutoff = datetime.fromisoformat(timestamp)
    except ValueError as error:
        raise ValueError("as_of_timestamp is not a valid timestamp.") from error

    local_cutoff = cutoff.astimezone(ZoneInfo(TIMEZONE))

    if cutoff.utcoffset() != local_cutoff.utcoffset():
        raise ValueError(
            "as_of_timestamp offset must match America/Toronto "
            "at that instant."
        )

    if local_cutoff.date() != parsed_date:
        raise ValueError(
            "as_of_timestamp must fall on episode_date in America/Toronto."
        )

    return context