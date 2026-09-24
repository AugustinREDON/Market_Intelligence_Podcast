from pathlib import Path

VOICE_MAP = {
    "HOST_A": "voices/Daniel_Mercer/DanielMercer.wav",
    "HOST_B": "voices/Maya_Brooks/MayaBrooks.wav",
}

DRIVE_SCRIPTS_DIR = (
    Path.home()
    / "Library"
    / "CloudStorage"
    / "GoogleDrive-augustin.redon@gmail.com"
    / "My Drive"
    / "Market Intelligence Podcast"
    / "Scripts"
)

DRIVE_MAIN_FOLDER_ID = "1_tLQxIQs_LBNDFYzu42o4n2A0SWpaAe4"

DRIVE_EPISODES_FOLDER_ID = "1M9YDC0gdOHwmuAPPhMme3a9B9ikInfqo"

CLIPS_DIR = "clips"
EPISODES_DIR = "episodes"
LOGS_DIR = "logs"

PAUSE_SECONDS = 0

DEVICE = "mps"

MP3_BITRATE = "192k"

MP3_RETENTION_DAYS = 14