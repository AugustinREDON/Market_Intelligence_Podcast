"""Render verified script text into isolated CUDA audio outputs.

Importing this module does not load Chatterbox or start generation.
"""

import json
import shutil
import subprocess
import time
from pathlib import Path

from cloud.acceptance import text_sha256
from cloud.output_validation import validate_script_for_acceptance


def render_episode(
    *,
    script,
    expected_script_sha256,
    voice_paths,
    output_dir,
):
    """Generate once in a new directory; retain evidence on failure.

    The caller must first verify acceptance with load_accepted_script().
    This function does not access S3, publish, retry, or stop EC2.
    """
    if text_sha256(script) != expected_script_sha256:
        raise ValueError("Renderer script hash does not match acceptance")

    validated = validate_script_for_acceptance(script)
    dialogue = []
    for raw_line in script.splitlines():
        line = raw_line.strip()
        if line:
            speaker, _, text = line.partition(":")
            dialogue.append((speaker, text.strip()))

    if set(voice_paths) != {"HOST_A", "HOST_B"}:
        raise ValueError("Both HOST_A and HOST_B voice paths are required")

    voices = {}
    for speaker, value in voice_paths.items():
        path = Path(value).resolve()
        if not path.is_file() or path.stat().st_size == 0:
            raise ValueError(f"Missing or empty voice reference: {speaker}")
        voices[speaker] = path

    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        raise RuntimeError("FFmpeg is required")

    # Heavy dependencies are imported only when rendering is requested.
    import torch
    import torchaudio
    from chatterbox.tts import ChatterboxTTS

    if not torch.cuda.is_available():
        raise RuntimeError("CUDA GPU is required for cloud rendering")

    output_dir = Path(output_dir).resolve()
    # Never reuse or clear a previous attempt directory.
    output_dir.mkdir(parents=True, exist_ok=False)
    clips_dir = output_dir / "clips"
    clips_dir.mkdir()
    references_dir = output_dir / "voice_derivatives"
    references_dir.mkdir()

    started = time.perf_counter()
    report = {
        "status": "running",
        "script_sha256": expected_script_sha256,
        "spoken_word_count": validated["spoken_word_count"],
        "expected_dialogue_lines": len(dialogue),
        "completed_dialogue_lines": 0,
        "device": "cuda",
        "gpu": torch.cuda.get_device_name(0),
        "torch_version": str(torch.__version__),
        "cuda_version": torch.version.cuda,
        "pause_seconds": 0,
        "mp3_bitrate": "192k",
        "line_timings": [],
    }

    def run_ffmpeg(arguments, *, timeout):
        with (output_dir / "ffmpeg.log").open("ab") as log:
            subprocess.run(
                [ffmpeg, "-nostdin", "-hide_banner", "-n", *arguments],
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=subprocess.STDOUT,
                check=True,
                timeout=timeout,
            )

    try:
        (output_dir / "script.md").write_bytes(script.encode("utf-8"))

        # Decode original MP3/WAV samples into mono PCM WAV references.
        for speaker, source in voices.items():
            destination = references_dir / f"{speaker.lower()}.wav"
            run_ffmpeg(
                [
                    "-i", str(source),
                    "-vn", "-ac", "1",
                    "-codec:a", "pcm_s16le",
                    str(destination),
                ],
                timeout=120,
            )
            voices[speaker] = destination

        stage_started = time.perf_counter()
        model = ChatterboxTTS.from_pretrained(device="cuda")
        torch.cuda.synchronize()
        report["model_load_seconds"] = time.perf_counter() - stage_started
        sample_rate = int(model.sr)
        generated = []

        for number, (speaker, text) in enumerate(dialogue, start=1):
            print(
                f"Generating line {number}/{len(dialogue)}: {speaker}",
                flush=True,
            )
            line_started = time.perf_counter()

            with torch.inference_mode():
                audio = model.generate(
                    text,
                    audio_prompt_path=str(voices[speaker]),
                )

            audio = audio.detach().to(device="cpu", dtype=torch.float32)
            if audio.ndim == 1:
                audio = audio.unsqueeze(0)
            if (
                audio.ndim != 2
                or audio.shape[0] != 1
                or audio.shape[1] == 0
                or not bool(torch.isfinite(audio).all())
            ):
                raise ValueError(f"Invalid generated audio at line {number}")

            clip = clips_dir / f"line_{number:04d}_{speaker}.wav"
            torchaudio.save(
                str(clip),
                audio,
                sample_rate,
                encoding="PCM_S",
                bits_per_sample=16,
            )
            generated.append(clip)
            report["completed_dialogue_lines"] = number
            report["line_timings"].append({
                "line": number,
                "speaker": speaker,
                "generation_and_save_seconds": (
                    time.perf_counter() - line_started
                ),
                "duration_seconds": audio.shape[1] / sample_rate,
            })

        # Join saved clips in their original dialogue order, with no pause.
        import wave

        wav_path = output_dir / "episode.wav"
        total_frames = 0
        with wave.open(str(wav_path), "wb") as episode:
            episode.setnchannels(1)
            episode.setsampwidth(2)
            episode.setframerate(sample_rate)
            for clip in generated:
                with wave.open(str(clip), "rb") as source:
                    if (
                        source.getnchannels() != 1
                        or source.getsampwidth() != 2
                        or source.getframerate() != sample_rate
                        or source.getcomptype() != "NONE"
                    ):
                        raise ValueError(f"Unexpected clip format: {clip.name}")
                    total_frames += source.getnframes()
                    episode.writeframes(source.readframes(source.getnframes()))

        mp3_path = output_dir / "episode.mp3"
        run_ffmpeg(
            [
                "-i", str(wav_path),
                "-codec:a", "libmp3lame",
                "-b:a", "192k",
                str(mp3_path),
            ],
            timeout=300,
        )
        if not mp3_path.is_file() or mp3_path.stat().st_size == 0:
            raise RuntimeError("FFmpeg produced no usable MP3 file")

        report.update({
            "status": "render_completed",
            "sample_rate": sample_rate,
            "duration_seconds": total_frames / sample_rate,
            "wav_filename": wav_path.name,
            "mp3_filename": mp3_path.name,
        })
        return report

    except Exception as error:
        report.update({
            "status": "failed",
            "error_type": type(error).__name__,
            "error": str(error),
        })
        raise

    finally:
        report["renderer_elapsed_seconds"] = time.perf_counter() - started
        with (output_dir / "render_result.json").open(
            "x", encoding="utf-8"
        ) as stream:
            json.dump(report, stream, indent=2, allow_nan=False)
            stream.write("\n")