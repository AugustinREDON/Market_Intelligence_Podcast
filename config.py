import os
from pathlib import Path

from dotenv import load_dotenv


PROJECT_DIR = Path(__file__).resolve().parent

# Load machine-specific configuration from the local .env file.
load_dotenv(PROJECT_DIR / ".env")


def require_env(name):
    value = os.getenv(name)

    if not value:
        raise RuntimeError(
            f"Missing required environment variable: {name}"
        )

    return value


VOICE_MAP = {
    "HOST_A": require_env("HOST_A_VOICE_PATH"),
    "HOST_B": require_env("HOST_B_VOICE_PATH"),
}

DRIVE_SCRIPTS_DIR = Path(
    require_env("DRIVE_SCRIPTS_DIR")
).expanduser()

DRIVE_MAIN_FOLDER_ID = require_env(
    "DRIVE_MAIN_FOLDER_ID"
)

DRIVE_EPISODES_FOLDER_ID = require_env(
    "DRIVE_EPISODES_FOLDER_ID"
)

CLIPS_DIR = "clips"
EPISODES_DIR = "episodes"
LOGS_DIR = "logs"

PAUSE_SECONDS = 0

DEVICE = "mps"

MP3_BITRATE = "192k"

MP3_RETENTION_DAYS = 14