import sys
import torchaudio
import os
import glob
import torch
import time
from pathlib import Path
from datetime import datetime
from chatterbox.tts import ChatterboxTTS

from config import (
    VOICE_MAP,
    CLIPS_DIR,
    EPISODES_DIR,
    LOGS_DIR,
    PAUSE_SECONDS,
    DEVICE,
)

# -----------------------------
# SETTINGS
# -----------------------------

if len(sys.argv) < 2:
    raise RuntimeError(
        "No script provided. Usage: .venv/bin/python generate_episode.py scripts/<script>.md"
    )

script_path = sys.argv[1]

if not os.path.exists(script_path):
    raise FileNotFoundError(f"Script not found: {script_path}")


# -----------------------------
# INITIALIZE LOGGING
# -----------------------------

os.makedirs(LOGS_DIR, exist_ok=True)

run_timestamp = datetime.now().strftime("%Y-%m-%d_%H:%M")

log_path = os.path.join(
    LOGS_DIR,
    f"podcast_run_{run_timestamp}.log"
)


def log(message=""):
    print(message)

    with open(log_path, "a", encoding="utf-8") as log_file:
        log_file.write(str(message) + "\n")


log("MARKET INTELLIGENCE PODCAST")
log(f"Started: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
log(f"Script: {script_path}")
log()


# -----------------------------
# VALIDATE SCRIPT
# -----------------------------

valid_lines = []
errors = []

with open(script_path, "r", encoding="utf-8") as file:
    for source_line_number, raw_line in enumerate(file, start=1):
        line = raw_line.strip()

        if not line:
            continue

        if line.startswith("HOST_A:"):
            speaker = "HOST_A"
            text = line[len("HOST_A:"):].strip()

        elif line.startswith("HOST_B:"):
            speaker = "HOST_B"
            text = line[len("HOST_B:"):].strip()

        else:
            errors.append(
                f"Line {source_line_number}: invalid speaker label -> {line}"
            )
            continue

        if not text:
            errors.append(
                f"Line {source_line_number}: {speaker} has no dialogue."
            )
            continue

        valid_lines.append((speaker, text))


if errors:
    log("SCRIPT VALIDATION FAILED")
    log()

    for error in errors:
        log(error)

    log()
    log(f"Validation errors: {len(errors)}")

    raise RuntimeError(
        f"Script contains {len(errors)} validation error(s)."
    )


if not valid_lines:
    log("SCRIPT VALIDATION FAILED")
    log("Script contains no valid dialogue.")

    raise RuntimeError("Script contains no valid dialogue.")


log("Script validation passed.")
log(f"Dialogue lines: {len(valid_lines)}")
log()


# -----------------------------
# SETUP
# -----------------------------

os.makedirs(CLIPS_DIR, exist_ok=True)
os.makedirs(EPISODES_DIR, exist_ok=True)

# Delete old clips before generating a new episode
old_clips = glob.glob(os.path.join(CLIPS_DIR, "*.wav"))

for clip in old_clips:
    os.remove(clip)

log(f"Cleared {len(old_clips)} old clips.")
log()


# -----------------------------
# LOAD MODEL
# -----------------------------

start_time = time.time()

model = ChatterboxTTS.from_pretrained(device=DEVICE)

line_number = 1

# Keep track ONLY of clips created during this run
generated_clips = []


# -----------------------------
# GENERATE AUDIO
# -----------------------------

for speaker, text in valid_lines:

    voice_path = VOICE_MAP[speaker]

    log(f"Generating line {line_number}...")
    log(f"Speaker: {speaker}")
    log(f"Voice: {voice_path}")
    log(f"Text: {text}")

    try:
        wav = model.generate(
            text,
            audio_prompt_path=voice_path
        )

        output_path = os.path.join(
            CLIPS_DIR,
            f"line_{line_number:04d}_{speaker}.wav"
        )

        torchaudio.save(
            output_path,
            wav,
            model.sr
        )

    except Exception as error:
        log()
        log("GENERATION FAILED")
        log(f"Line: {line_number}")
        log(f"Speaker: {speaker}")
        log(f"Text: {text}")
        log(f"Error: {error}")
        log(f"Clips completed before failure: {len(generated_clips)}")
        log("No final episode was created.")

        sys.exit(1)

    generated_clips.append(output_path)

    log(f"Created: {output_path}")
    log()

    line_number += 1


# -----------------------------
# VALIDATE GENERATED CLIPS
# -----------------------------

if not generated_clips:
    log("GENERATION FAILED")
    log("No podcast lines were generated.")

    raise RuntimeError("No podcast lines were generated.")


if len(generated_clips) != len(valid_lines):
    log("GENERATION FAILED")
    log(
        f"Clip count mismatch: expected {len(valid_lines)}, "
        f"generated {len(generated_clips)}."
    )

    raise RuntimeError(
        f"Clip count mismatch: expected {len(valid_lines)}, "
        f"generated {len(generated_clips)}."
    )


log(f"Generated {len(generated_clips)} clips successfully.")
log()


# -----------------------------
# STITCH EPISODE
# -----------------------------

all_audio = []

pause_samples = int(model.sr * PAUSE_SECONDS)
pause = torch.zeros(1, pause_samples)

for i, clip_file in enumerate(generated_clips):

    wav, sample_rate = torchaudio.load(clip_file)

    all_audio.append(wav)

    # Add a pause after every clip except the last one
    if i < len(generated_clips) - 1:
        all_audio.append(pause)


combined_wav = torch.cat(all_audio, dim=1)


# -----------------------------
# SAVE FINAL EPISODE
# -----------------------------

script_name = Path(script_path).stem

episode_output = os.path.join(
    EPISODES_DIR,
    f"{script_name}.wav"
)

torchaudio.save(
    episode_output,
    combined_wav,
    model.sr
)


# -----------------------------
# RESULTS
# -----------------------------

elapsed_time = time.time() - start_time
duration_seconds = combined_wav.shape[1] / model.sr

log()
log("SUCCESS")
log(f"Episode: {episode_output}")
log(f"Clips generated: {len(generated_clips)}")
log(f"Episode duration: {duration_seconds / 60:.1f} minutes")
log(f"Pipeline time: {elapsed_time / 60:.1f} minutes")