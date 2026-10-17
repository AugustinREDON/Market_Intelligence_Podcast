from pathlib import Path

from audio_config import (
    PROJECT_DIR,
    require_env,
    VOICE_MAP,
    CLIPS_DIR,
    EPISODES_DIR,
    LOGS_DIR,
    PAUSE_SECONDS,
    DEVICE,
    MP3_BITRATE,
    MP3_RETENTION_DAYS,
)


DRIVE_SCRIPTS_DIR = Path(
    require_env("DRIVE_SCRIPTS_DIR")
).expanduser()

DRIVE_MAIN_FOLDER_ID = require_env(
    "DRIVE_MAIN_FOLDER_ID"
)

DRIVE_EPISODES_FOLDER_ID = require_env(
    "DRIVE_EPISODES_FOLDER_ID"
)

SCRIPT_WAIT_TIMEOUT_SECONDS = 600
SCRIPT_RETRY_INTERVAL_SECONDS = 15