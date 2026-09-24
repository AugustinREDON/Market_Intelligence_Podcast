from pathlib import Path
from datetime import datetime
import shutil
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

    shutil.copy2(
        drive_script_path,
        local_script_path,
    )

    return local_script_path

def fetch_today_script():
    drive_script_path = get_drive_script_path()

    validate_script(drive_script_path)

    local_script_path = copy_script_locally(
        drive_script_path
    )

    validate_script(local_script_path)

    return local_script_path