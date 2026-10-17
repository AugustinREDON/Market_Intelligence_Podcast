import os
from pathlib import Path

from dotenv import load_dotenv


PROJECT_DIR = Path(__file__).resolve().parent
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

CLIPS_DIR = "clips"
EPISODES_DIR = "episodes"
LOGS_DIR = "logs"

PAUSE_SECONDS = 0
DEVICE = os.getenv("TTS_DEVICE", "mps")

MP3_BITRATE = "192k"
MP3_RETENTION_DAYS = 14