"""Offline checks for completion identity and artifact manifests."""

import copy
from datetime import datetime, timezone, timedelta
import unittest

from cloud.acceptance import json_sha256
from cloud.audio_completion import validate_audio_success


class AudioCompletionTests(unittest.TestCase):
    def setUp(self):
        self.run_id = "2026-11-12-997"
        self.script_hash = "b" * 64
        self.job = {
            "run_id": self.run_id, "bucket": "offline-test", "region": "us-east-2",
            "voices": {
                speaker: {
                    "key": f"assets/voices/v1/{speaker.lower()}.mp3",
                    "sha256": "c" * 64,
                }
                for speaker in ("HOST_A", "HOST_B")
            },
        }
        self.claim = {
            "run_id": self.run_id, "attempt_id": "a" * 32,
            "status": "audio_submission_claimed",
            "script_sha256": self.script_hash,
            "voices": copy.deepcopy(self.job["voices"]),
            "automatic_retry_allowed": False,
        }
        self.prefix = f'runs/{self.run_id}/audio/attempts/{self.claim["attempt_id"]}/'
        now = datetime.now(timezone.utc)
        self.startup = (now - timedelta(minutes=20)).isoformat()
        names = (
            "audio_job.json", "verified_script_acceptance.json", "job_result.json",
            "render/script.md", "render/render_result.json",
            "render/episode.mp3", "render/episode.wav",
        )
        self.success = {
            "run_id": self.run_id, "attempt_id": self.claim["attempt_id"],
            "s3_prefix": self.prefix, "status": "audio_completed",
            "script_sha256": self.script_hash,
            "automatic_retry_allowed": False, "claim_confirmed": True,
            "duration_seconds": 832.04,
            "started_at": (now - timedelta(minutes=19)).isoformat(),
            "finished_at": (now - timedelta(minutes=1)).isoformat(),
            "mp3_key": self.prefix + "render/episode.mp3",
            "wav_key": self.prefix + "render/episode.wav",
            "artifacts": [
                {
                    "key": self.prefix + name,
                    "bytes": 123,
                    "sha256": (
                        self.script_hash if name == "render/script.md"
                        else "d" * 64
                    ),
                }
                for name in names
            ],
        }

    def verify(self, **changes):
        return validate_audio_success(**{
            "run_id": self.run_id, "job": self.job,
            "job_sha256": json_sha256(self.job), "script_sha256": self.script_hash,
            "startup_requested_at": self.startup,
            "claim": self.claim, "success": self.success, **changes,
        })

    def test_matching_manifest_returns_only_required_artifacts(self):
        result = self.verify()
        self.assertEqual(result["status"], "audio_success_manifest_verified")
        self.assertEqual(result["success_key"], self.prefix + "audio_success.json")
        self.assertEqual(len(result["artifacts"]), 7)
        self.assertEqual(result["mp3_key"], self.success["mp3_key"])

    def test_wrong_claim_or_completion_identity_is_rejected(self):
        for record, field, value in (
            ("claim", "run_id", "2026-11-12-996"),
            ("claim", "script_sha256", "e" * 64),
            ("claim", "voices", {}),
            ("claim", "automatic_retry_allowed", 0),
            ("success", "attempt_id", "f" * 32),
            ("success", "mp3_key", "another/episode.mp3"),
            ("success", "status", "render_completed"),
            ("success", "claim_confirmed", 1),
        ):
            with self.subTest(record=record, field=field):
                changed = copy.deepcopy(getattr(self, record))
                changed[field] = value
                with self.assertRaises(ValueError):
                    self.verify(**{record: changed})

    def test_invalid_inventory_is_rejected(self):
        for scenario in (
            "missing", "duplicate", "outside", "path",
            "hash", "size", "empty", "script",
        ):
            with self.subTest(scenario=scenario):
                changed = copy.deepcopy(self.success)
                entries = changed["artifacts"]
                if scenario == "missing":
                    entries.pop()
                elif scenario == "duplicate":
                    entries.append(copy.deepcopy(entries[0]))
                elif scenario == "outside":
                    entries[0]["key"] = "another/audio_job.json"
                elif scenario == "path":
                    entries[0]["key"] = self.prefix + "../audio_job.json"
                elif scenario == "hash":
                    entries[0]["sha256"] = "invalid"
                elif scenario == "size":
                    entries[0]["bytes"] = True
                elif scenario == "empty":
                    entries[-1]["bytes"] = 0
                else:
                    entries[3]["sha256"] = "e" * 64
                with self.assertRaises(ValueError):
                    self.verify(success=changed)

    def test_invalid_duration_and_timestamps_are_rejected(self):
        for field, value in (
            ("duration_seconds", True), ("duration_seconds", 0),
            ("duration_seconds", float("nan")),
            ("finished_at", "2026-11-12T10:00:00"),
            ("started_at", "2020-01-01T00:00:00+00:00"),
            (
                "finished_at",
                (datetime.now(timezone.utc) + timedelta(days=1)).isoformat(),
            ),
        ):
            with self.subTest(field=field, value=value):
                changed = {**self.success, field: value}
                with self.assertRaises(ValueError):
                    self.verify(success=changed)

    def test_job_must_match_the_prepared_hash(self):
        with self.assertRaises(ValueError):
            self.verify(job_sha256="0" * 64)
        changed = {**self.job, "bucket": "another-bucket"}
        with self.assertRaises(ValueError):
            self.verify(job=changed, job_sha256=json_sha256(self.job))


if __name__ == "__main__":
    unittest.main()