"""Offline preflight tests; no GPU, model, AWS, or real FFmpeg calls."""

import sys
import tempfile
import unittest
import wave
from pathlib import Path
from types import ModuleType
from unittest.mock import Mock, patch

from cloud.audio_renderer import preflight_audio


class AudioPreflightTests(unittest.TestCase):
    def setUp(self):
        root = Path("local_runs/audio/offline_tests")
        root.mkdir(parents=True, exist_ok=True)
        temporary = tempfile.TemporaryDirectory(dir=root)
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

        self.voices = {
            speaker: self.root / f"{speaker.lower()}.wav"
            for speaker in ("HOST_A", "HOST_B")
        }
        for path in self.voices.values():
            self.write_wav(path)

        self.torch = ModuleType("torch")
        self.torch.cuda = Mock()
        self.torch.cuda.is_available.return_value = True
        self.torch.cuda.get_device_name.return_value = "Offline GPU"
        self.torch.__version__ = "offline"
        self.torch.version = Mock(cuda="offline")
        self.torch.ones = Mock()
        self.torch.ones.return_value.add.return_value.item.return_value = 2
        self.torch.zeros = Mock()

        self.torchaudio = ModuleType("torchaudio")
        self.torchaudio.save = Mock(
            side_effect=lambda path, *args, **kwargs: self.write_wav(path)
        )
        self.model = Mock()
        chatterbox = ModuleType("chatterbox")
        chatterbox.__path__ = []
        tts = ModuleType("chatterbox.tts")
        tts.ChatterboxTTS = self.model

        modules = patch.dict(sys.modules, {
            "torch": self.torch,
            "torchaudio": self.torchaudio,
            "chatterbox": chatterbox,
            "chatterbox.tts": tts,
        })
        modules.start()
        self.addCleanup(modules.stop)

        executable = patch(
            "cloud.audio_renderer.shutil.which",
            return_value="/offline/ffmpeg",
        )
        self.executable = executable.start()
        self.addCleanup(executable.stop)

        process = patch(
            "cloud.audio_renderer.subprocess.run",
            side_effect=self.fake_ffmpeg,
        )
        self.process = process.start()
        self.addCleanup(process.stop)

    @staticmethod
    def write_wav(path):
        with wave.open(str(path), "wb") as output:
            output.setnchannels(1)
            output.setsampwidth(2)
            output.setframerate(24000)
            output.writeframes(b"\x00\x00" * 2400)

    def fake_ffmpeg(self, command, **kwargs):
        destination = Path(command[-1])
        if destination.suffix == ".wav":
            self.write_wav(destination)
        else:
            destination.write_bytes(b"offline encoder output")

    def run_preflight(self):
        return preflight_audio(
            voice_paths=self.voices,
            check_dir=self.root / "preflight",
        )

    def test_success_checks_runtime_without_loading_model(self):
        result = self.run_preflight()

        self.assertEqual(result["status"], "passed")
        self.assertEqual(set(result["voices"]), {"HOST_A", "HOST_B"})
        self.assertFalse(result["model_loaded"])
        self.assertFalse(result["generation_requested"])
        self.torch.cuda.synchronize.assert_called_once()
        self.torchaudio.save.assert_called_once()
        self.assertEqual(self.process.call_count, 3)
        self.model.from_pretrained.assert_not_called()

    def test_missing_ffmpeg_stops(self):
        self.executable.return_value = None

        with self.assertRaisesRegex(RuntimeError, "FFmpeg is required"):
            self.run_preflight()

        self.process.assert_not_called()
        self.model.from_pretrained.assert_not_called()

    def test_missing_cuda_stops(self):
        self.torch.cuda.is_available.return_value = False

        with self.assertRaisesRegex(RuntimeError, "CUDA GPU is required"):
            self.run_preflight()

        self.torch.ones.assert_not_called()
        self.process.assert_not_called()

    def test_voice_decoding_failure_stops(self):
        def fail_voice_decode(command, **kwargs):
            if Path(command[-1]).name == "host_a.wav":
                raise RuntimeError("Voice decoding failed")
            self.fake_ffmpeg(command, **kwargs)

        self.process.side_effect = fail_voice_decode

        with self.assertRaisesRegex(RuntimeError, "Voice decoding failed"):
            self.run_preflight()

        self.model.from_pretrained.assert_not_called()

    def test_existing_check_directory_is_preserved(self):
        directory = self.root / "preflight"
        directory.mkdir()
        evidence = directory / "existing.txt"
        evidence.write_text("preserve", encoding="utf-8")

        with self.assertRaises(FileExistsError):
            self.run_preflight()

        self.assertEqual(evidence.read_text(encoding="utf-8"), "preserve")
        self.process.assert_not_called()


if __name__ == "__main__":
    unittest.main()