from pathlib import Path
from datetime import datetime
import errno
import shutil
import time
from config import DRIVE_SCRIPTS_DIR

PROJECT_DIR = Path(__file__).resolve().parent
LOCAL_SCRIPTS_DIR = PROJECT_DIR / "scripts"

def get_today_script_name():
    today = datetime.now().strftime("%Y-%m-%d")
    return f"MIP_script_{today}.md"

def get_drive_script_path():
    script_name = get_today_script_name()
    drive_script_path = DRIVE_SCRIPTS_DIR / script_name

    if not drive_script_path.exists():
        raise FileNotFoundError(
            f"Today's script was not found in Google Drive: {drive_script_path}"
        )

    return drive_script_path

def validate_script(script_path):
    if script_path.stat().st_size == 0:
        raise ValueError(
            f"Script is empty: {script_path}"
        )


def copy_script_locally(drive_script_path):
    LOCAL_SCRIPTS_DIR.mkdir(parents=True, exist_ok=True)

    local_script_path = LOCAL_SCRIPTS_DIR / drive_script_path.name

    max_attempts = 4
    retry_delay_seconds = 3

    for attempt in range(1, max_attempts + 1):
        try:
            # Read the file first to force Google Drive/macOS
            # to make its contents locally available.
            with drive_script_path.open("rb") as source:
                while source.read(1024 * 1024):
                    pass

            shutil.copy2(
                drive_script_path,
                local_script_path,
            )

            return local_script_path

        except OSError as exc:
            retryable_errors = {
                11,
                errno.EAGAIN,
                errno.EDEADLK,
            }

            if exc.errno not in retryable_errors or attempt == max_attempts:
                raise

            # Remove any incomplete destination left by a failed copy.
            if local_script_path.exists():
                local_script_path.unlink()

            time.sleep(retry_delay_seconds)

def fetch_today_script():
    drive_script_path = get_drive_script_path()

    validate_script(drive_script_path)

    local_script_path = copy_script_locally(
        drive_script_path
    )

    validate_script(local_script_path)

    return local_script_path