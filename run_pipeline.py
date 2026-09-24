import subprocess
import re
import sys
from pathlib import Path
from datetime import datetime, timedelta
from drive_upload import upload_episode, cleanup_old_drive_episodes
from config  import MP3_BITRATE, MP3_RETENTION_DAYS
from script_ingestion import fetch_today_script


# -----------------------------
# PATHS
# -----------------------------

PROJECT_DIR = Path(__file__).resolve().parent
SCRIPTS_DIR = PROJECT_DIR / "scripts"
GENERATE_EPISODE = PROJECT_DIR / "generate_episode.py"


# -----------------------------
# FIND LATEST SCRIPT -- UNUSED FOR NOW, REPLACED BY fetch_today_script()
# -----------------------------

def find_latest_script():
    today = datetime.now().strftime("%Y-%m-%d")

    all_scripts = list(SCRIPTS_DIR.glob("*.md"))

    # 1. Prioritize scripts dated today
    today_scripts = [
        path
        for path in all_scripts
        if today in path.stem
    ]

    if today_scripts:
        return max(
            today_scripts,
            key=lambda path: path.stat().st_mtime
        )

    # 2. If no script exists for today, extract dates from all filenames
    dated_scripts = []

    for path in all_scripts:
        match = re.search(r"\d{4}-\d{2}-\d{2}", path.stem)

        if match:
            script_date = datetime.strptime(
                match.group(),
                "%Y-%m-%d"
            ).date()

            dated_scripts.append((path, script_date))

    if not dated_scripts:
        raise FileNotFoundError(
            f"No dated podcast scripts found in: {SCRIPTS_DIR}"
        )

    # 3. Find the latest date represented in the filenames
    latest_date = max(
        script_date
        for _, script_date in dated_scripts
    )

    # 4. Get all scripts belonging to that latest date
    latest_date_scripts = [
        path
        for path, script_date in dated_scripts
        if script_date == latest_date
    ]

    # 5. If multiple exist for that date, use the most recently modified
    latest_script = max(
        latest_date_scripts,
        key=lambda path: path.stat().st_mtime
    )

    return latest_script

# -----------------------------
# RUN EPISODE GENERATOR
# -----------------------------

def generate_episode(script_path):
    print(f"\nSelected script: {script_path.name}")
    print("Starting episode generation...\n")

    result = subprocess.run(
        [
            sys.executable,
            str(GENERATE_EPISODE),
            str(script_path),
        ]
    )

    if result.returncode != 0:
        print("\nEpisode generation failed.")
        sys.exit(result.returncode)

    episode_path = (
        PROJECT_DIR
        / "episodes"
        / f"{script_path.stem}.wav"
    )

    if not episode_path.exists():
        raise FileNotFoundError(
            f"Generation completed, but episode was not found: {episode_path}"
        )

    return episode_path


# -----------------------------
# CONVERT WAV TO MP3
# -----------------------------

def convert_to_mp3(wav_path):
    mp3_path = wav_path.with_suffix(".mp3")

    print("\nConverting WAV to MP3...")

    result = subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-i",
            str(wav_path),
            "-codec:a",
            "libmp3lame",
            "-b:a",
            MP3_BITRATE,
            str(mp3_path),
        ],
        capture_output=True,
        text=True,
    )

    if result.returncode != 0:
        print("\nMP3 conversion failed.")
        print(result.stderr)
        sys.exit(result.returncode)

    if not mp3_path.exists():
        raise FileNotFoundError(
            f"Conversion completed, but MP3 was not found: {mp3_path}"
        )

    print(f"MP3 created: {mp3_path.name}")

    return mp3_path

# -----------------------------
# CLEAN UP OLD LOCAL MP3 FILES
# -----------------------------

def cleanup_old_mp3s():
    episodes_dir = PROJECT_DIR / "episodes"
    cutoff_date = datetime.now().date() - timedelta(
        days=MP3_RETENTION_DAYS
    )

    deleted_count = 0

    for mp3_path in episodes_dir.glob("*.mp3"):
        match = re.search(
            r"\d{4}-\d{2}-\d{2}",
            mp3_path.name,
        )

        if not match:
            continue

        episode_date = datetime.strptime(
            match.group(),
            "%Y-%m-%d",
        ).date()

        if episode_date < cutoff_date:
            mp3_path.unlink()
            deleted_count += 1
            print(
                f"Deleted old local MP3: "
                f"{mp3_path.name}"
            )

    if deleted_count == 0:
        print("No old local MP3s to delete.")


# -----------------------------
# MAIN PIPELINE
# -----------------------------

def main():

    print("=" * 50)
    print("MARKET INTELLIGENCE PODCAST PIPELINE")
    print("=" * 50)

    script_path = fetch_today_script()
    episode_path = generate_episode(script_path)

    mp3_path = convert_to_mp3(episode_path)

    print("\nUploading episode to Google Drive...")

    try:
        uploaded_file = upload_episode(mp3_path)

    except Exception as error:
        print("\nGoogle Drive upload failed.")
        print(f"Episode was generated successfully at: {episode_path}")
        print(f"Error: {error}")
        sys.exit(1)

    print("Google Drive upload successful.")
    print(f"Drive link: {uploaded_file['webViewLink']}")

    try:
        episode_path.unlink()
        print(f"Deleted local WAV: {episode_path.name}")

    except Exception as error:
        print(f"Warning: Could not delete local WAV: {error}")

    cleanup_old_mp3s()

    print("\nCleaning up old Google Drive episodes...")

    try:
        cleanup_old_drive_episodes()

    except Exception as error:
        print(
            f"Warning: Google Drive cleanup failed: {error}"
        )

    print("\n" + "=" * 50)
    print("PIPELINE COMPLETE")
    print(f"Episode: {mp3_path}")
    print("=" * 50)


if __name__ == "__main__":
    main()