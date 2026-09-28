from datetime import datetime
from pathlib import Path
import errno
import tempfile
import time

from config import (
    DRIVE_SCRIPTS_DIR,
    SCRIPT_WAIT_TIMEOUT_SECONDS,
    SCRIPT_RETRY_INTERVAL_SECONDS,
)


PROJECT_DIR = Path(__file__).resolve().parent
LOCAL_SCRIPTS_DIR = PROJECT_DIR / "scripts"

RETRYABLE_ERRORS = {
    errno.ENOENT,       # File is not available yet.
    errno.EAGAIN,       # Resource temporarily unavailable.
    errno.EDEADLK,      # Resource deadlock avoided.
    errno.EBUSY,        # Resource busy.
    errno.ETIMEDOUT,    # File Provider operation timed out.
}


class EmptyScriptError(ValueError):
    pass


def log(message):
    timestamp = datetime.now().isoformat(timespec="seconds")
    print(f"[{timestamp}] [script_ingestion] {message}", flush=True)


def get_today_script_name():
    today = datetime.now().strftime("%Y-%m-%d")
    return f"MIP_script_{today}.md"


def get_drive_script_path():
    # Availability is checked by reading inside the retry loop.
    return DRIVE_SCRIPTS_DIR / get_today_script_name()


def validate_script(script_path):
    if script_path.stat().st_size == 0:
        raise EmptyScriptError(f"Script is empty: {script_path}")


def copy_script_locally(drive_script_path):
    # Reading requests the cloud-backed contents from File Provider.
    # Read once, rather than reading and then copying the source again.
    contents = drive_script_path.read_bytes()

    if not contents:
        raise EmptyScriptError(f"Script is empty: {drive_script_path}")

    LOCAL_SCRIPTS_DIR.mkdir(parents=True, exist_ok=True)
    local_script_path = LOCAL_SCRIPTS_DIR / drive_script_path.name
    temporary_path = None

    try:
        # Write beside the destination so replacement is atomic.
        with tempfile.NamedTemporaryFile(
            mode="wb",
            dir=LOCAL_SCRIPTS_DIR,
            prefix=f".{drive_script_path.stem}-",
            suffix=".tmp",
            delete=False,
        ) as temporary_file:
            temporary_path = Path(temporary_file.name)
            temporary_file.write(contents)

        validate_script(temporary_path)
        temporary_path.replace(local_script_path)

    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)

    return local_script_path


def fetch_today_script():
    if SCRIPT_WAIT_TIMEOUT_SECONDS <= 0:
        raise ValueError("SCRIPT_WAIT_TIMEOUT_SECONDS must be positive.")

    if SCRIPT_RETRY_INTERVAL_SECONDS <= 0:
        raise ValueError("SCRIPT_RETRY_INTERVAL_SECONDS must be positive.")

    # Keep the target date fixed throughout this run.
    drive_script_path = get_drive_script_path()
    deadline = time.monotonic() + SCRIPT_WAIT_TIMEOUT_SECONDS
    attempt = 0

    log(f"Waiting for today's script: {drive_script_path}")

    while True:
        attempt += 1

        try:
            local_script_path = copy_script_locally(drive_script_path)

        except (OSError, EmptyScriptError) as exc:
            if isinstance(exc, OSError) and exc.errno not in RETRYABLE_ERRORS:
                raise

            remaining = deadline - time.monotonic()

            if remaining <= 0:
                raise TimeoutError(
                    f"Today's script could not be retrieved within "
                    f"{SCRIPT_WAIT_TIMEOUT_SECONDS} seconds after "
                    f"{attempt} attempts: {drive_script_path}. "
                    f"Last error: {exc}"
                ) from exc

            delay = min(SCRIPT_RETRY_INTERVAL_SECONDS, remaining)
            log(
                f"Attempt {attempt} failed: {exc}. "
                f"Retrying in {delay:.0f}s "
                f"({remaining:.0f}s remaining)."
            )
            time.sleep(delay)

        else:
            log(f"Script ready: {local_script_path}")
            return local_script_path