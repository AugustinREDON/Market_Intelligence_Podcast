"""Offline checks for audio job claims and output handling."""

import copy
import hashlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from botocore.exceptions import ClientError

from cloud.audio_job import execute_job, validate_job


class AudioJobTests(unittest.TestCase):
    def setUp(self):
        test_root = Path("local_runs/audio/offline_tests")
        test_root.mkdir(parents=True, exist_ok=True)
        temporary = tempfile.TemporaryDirectory(dir=test_root)
        self.addCleanup(temporary.cleanup)
        self.work_root = Path(temporary.name)

        self.voice_bytes = b"offline voice fixture"
        digest = hashlib.sha256(self.voice_bytes).hexdigest()
        self.job = {
            "run_id": "2026-10-28-002",
            "bucket": "offline-test",
            "region": "us-east-2",
            "voices": {
                "HOST_A": {
                    "key": "assets/voices/v1/host_a.mp3",
                    "sha256": digest,
                },
                "HOST_B": {
                    "key": "assets/voices/v1/host_b.mp3",
                    "sha256": digest,
                },
            },
        }
        self.claim_key = (
            "runs/2026-10-28-002/audio_submission_claim.json"
        )
        self.objects = {}
        self.write_order = []
        self.client = Mock()
        self.client.get_object.side_effect = lambda **kwargs: {
            "Body": io.BytesIO(self.voice_bytes)
        }
        self.client.put_object.side_effect = self.put_object

        script = "HOST_A: Offline fixture\nHOST_B: Reply"
        self.accepted = {
            "script": script,
            "acceptance": {
                "script_sha256": hashlib.sha256(
                    script.encode("utf-8")
                ).hexdigest(),
            },
        }

        loader = patch(
            "cloud.audio_job.load_accepted_script",
            return_value=self.accepted,
        )
        self.loader = loader.start()
        self.addCleanup(loader.stop)

        preflight = patch(
            "cloud.audio_job.preflight_audio",
            return_value={"status": "passed"},
        )
        self.preflight = preflight.start()
        self.addCleanup(preflight.stop)

        renderer = patch(
            "cloud.audio_job.render_episode",
            side_effect=self.fake_render,
        )
        self.renderer = renderer.start()
        self.addCleanup(renderer.stop)

    def put_object(self, **kwargs):
        self.assertEqual(kwargs["IfNoneMatch"], "*")
        key = kwargs["Key"]
        if key in self.objects:
            raise ClientError(
                {
                    "Error": {"Code": "PreconditionFailed"},
                    "ResponseMetadata": {"HTTPStatusCode": 412},
                },
                "PutObject",
            )
        body = kwargs["Body"]
        self.objects[key] = (
            body.read() if hasattr(body, "read") else body
        )
        self.write_order.append(key)

    def fake_render(self, **kwargs):
        directory = Path(kwargs["output_dir"])
        directory.mkdir()
        (directory / "episode.wav").write_bytes(b"offline WAV")
        (directory / "episode.mp3").write_bytes(b"offline MP3")
        (directory / "render_result.json").write_text(
            '{"status": "render_completed"}',
            encoding="utf-8",
        )
        return {"duration_seconds": 12.5}

    def run_job(self):
        with patch("builtins.print"):
            execute_job(
                job=self.job,
                client=self.client,
                work_root=self.work_root,
            )

    def success_keys(self):
        return [
            key for key in self.objects
            if key.endswith("/audio_success.json")
        ]

    def test_success_marker_is_written_last(self):
        self.run_job()

        self.renderer.assert_called_once()
        arguments = self.renderer.call_args.kwargs
        self.assertEqual(arguments["script"], self.accepted["script"])
        self.assertEqual(
            arguments["expected_script_sha256"],
            self.accepted["acceptance"]["script_sha256"],
        )
        self.assertEqual(len(self.success_keys()), 1)
        self.assertEqual(self.write_order[-1], self.success_keys()[0])

        success = json.loads(self.objects[self.success_keys()[0]])
        self.assertEqual(success["status"], "audio_completed")
        self.assertIn(success["mp3_key"], self.objects)
        self.assertIn(success["wav_key"], self.objects)

    def test_preflight_runs_before_claim(self):
        def check_before_claim(**kwargs):
            self.assertNotIn(self.claim_key, self.objects)
            self.renderer.assert_not_called()
            self.assertEqual(
                set(kwargs["voice_paths"]),
                {"HOST_A", "HOST_B"},
            )
            for path in kwargs["voice_paths"].values():
                self.assertTrue(Path(path).is_file())
            return {"status": "passed"}

        self.preflight.side_effect = check_before_claim
        self.run_job()

        self.preflight.assert_called_once()
        self.renderer.assert_called_once()
        self.assertIn(self.claim_key, self.objects)

    def test_preflight_failure_stops_before_claim(self):
        self.preflight.side_effect = RuntimeError("CUDA check failed")

        with self.assertRaisesRegex(RuntimeError, "CUDA check failed"):
            self.run_job()

        self.client.put_object.assert_not_called()
        self.renderer.assert_not_called()
        self.assertEqual(self.success_keys(), [])

        results = list(self.work_root.rglob("job_result.json"))
        self.assertEqual(len(results), 1)
        report = json.loads(results[0].read_text(encoding="utf-8"))
        self.assertEqual(report["status"], "failed")
        self.assertFalse(report["claim_confirmed"])


    def test_second_job_cannot_generate_again(self):
        self.run_job()
        claim = self.objects[self.claim_key]
        self.renderer.reset_mock()
        self.client.put_object.reset_mock()

        with self.assertRaises(ClientError):
            self.run_job()

        self.renderer.assert_not_called()
        self.client.put_object.assert_called_once()
        self.assertEqual(self.objects[self.claim_key], claim)

    def test_uncertain_claim_stops_without_generation_or_retry(self):
        self.client.put_object.side_effect = TimeoutError(
            "Uncertain claim response"
        )

        with self.assertRaises(TimeoutError):
            self.run_job()

        self.client.put_object.assert_called_once()
        self.renderer.assert_not_called()
        self.assertEqual(self.success_keys(), [])

    def test_changed_voice_stops_before_claim(self):
        self.job["voices"]["HOST_A"]["sha256"] = "0" * 64

        with self.assertRaisesRegex(ValueError, "Voice hash mismatch"):
            self.run_job()

        self.renderer.assert_not_called()
        self.client.put_object.assert_not_called()

    def test_acceptance_failure_stops_before_voice_download(self):
        self.loader.side_effect = ValueError("Acceptance mismatch")

        with self.assertRaisesRegex(ValueError, "Acceptance mismatch"):
            self.run_job()

        self.client.get_object.assert_not_called()
        self.client.put_object.assert_not_called()
        self.renderer.assert_not_called()

    def test_renderer_failure_preserves_claim_and_failure_evidence(self):
        self.renderer.side_effect = RuntimeError("Generation failed")

        with self.assertRaisesRegex(RuntimeError, "Generation failed"):
            self.run_job()

        self.assertIn(self.claim_key, self.objects)
        self.assertEqual(self.success_keys(), [])
        results = [
            json.loads(value)
            for key, value in self.objects.items()
            if key.endswith("/job_result.json")
        ]
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["status"], "failed")

    def test_upload_failure_prevents_success_marker(self):
        def fail_mp3_upload(**kwargs):
            if kwargs["Key"].endswith("/episode.mp3"):
                raise RuntimeError("Upload failed")
            return self.put_object(**kwargs)

        self.client.put_object.side_effect = fail_mp3_upload

        with self.assertRaisesRegex(RuntimeError, "Audio upload incomplete"):
            self.run_job()

        self.assertIn(self.claim_key, self.objects)
        self.assertEqual(self.success_keys(), [])
        evidence = list(self.work_root.rglob("upload_result.json"))
        self.assertEqual(len(evidence), 1)
        report = json.loads(evidence[0].read_text(encoding="utf-8"))
        self.assertEqual(len(report["errors"]), 1)

    def test_job_requires_versioned_mp3_keys_and_voice_hashes(self):
        self.assertEqual(validate_job(self.job), self.job)

        for field, value in (
            ("key", "assets/voices/host_a.mp3"),
            ("key", "assets/voices/v1/host_a.flac"),
            ("sha256", "missing"),
        ):
            with self.subTest(field=field, value=value):
                changed = copy.deepcopy(self.job)
                changed["voices"]["HOST_A"][field] = value
                with self.assertRaises(ValueError):
                    validate_job(changed)


if __name__ == "__main__":
    unittest.main()