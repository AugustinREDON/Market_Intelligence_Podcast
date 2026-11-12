"""Offline checks for completion identity and artifact manifests."""

import copy
from datetime import datetime, timezone, timedelta
import unittest
import hashlib
import io
import json

from botocore.exceptions import ClientError
from unittest.mock import Mock
from cloud.acceptance import json_sha256
from cloud.audio_completion import validate_audio_success, read_audio_completion


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

    def make_reader(self):
        script = b"HOST_A: Offline fixture\nHOST_B: Reply"
        self.script_hash = hashlib.sha256(script).hexdigest()
        self.claim["script_sha256"] = self.script_hash
        self.success["script_sha256"] = self.script_hash
        acceptance = {"script_sha256": self.script_hash, "dialogue_line_count": 2}
        report = {
            key: value for key, value in self.success.items()
            if key not in {"mp3_key", "wav_key", "artifacts"}
        }
        report["status"] = "render_completed"
        rendered = {
            "status": "render_completed", "script_sha256": self.script_hash,
            "duration_seconds": self.success["duration_seconds"],
            "expected_dialogue_lines": 2, "completed_dialogue_lines": 2,
        }
        encode = lambda value: json.dumps(value).encode()
        files = {
            "audio_job.json": encode(self.job),
            "verified_script_acceptance.json": encode(acceptance),
            "job_result.json": encode(report),
            "render/script.md": script,
            "render/render_result.json": encode(rendered),
            "render/episode.mp3": b"offline MP3",
            "render/episode.wav": b"offline WAV",
        }
        objects = {self.prefix + name: raw for name, raw in files.items()}
        for artifact in self.success["artifacts"]:
            raw = objects[artifact["key"]]
            artifact.update(bytes=len(raw), sha256=hashlib.sha256(raw).hexdigest())
        objects[self.prefix + "audio_success.json"] = encode(self.success)
        objects[f"runs/{self.run_id}/audio_submission_claim.json"] = encode(self.claim)
        client = Mock()

        def read(**kwargs):
            key = kwargs["Key"]
            if key not in objects:
                raise ClientError(
                    {"Error": {"Code": "NoSuchKey"},
                     "ResponseMetadata": {"HTTPStatusCode": 404}}, "GetObject"
                )
            raw = objects[key]
            return {"Body": io.BytesIO(raw), "ContentLength": len(raw)}

        client.get_object.side_effect = read
        arguments = {
            "run_id": self.run_id, "job": self.job,
            "job_sha256": json_sha256(self.job), "script_acceptance": acceptance,
            "startup_requested_at": self.startup, "client": client,
        }
        return arguments, objects

    def test_reader_verifies_required_files_without_writing(self):
        arguments, objects = self.make_reader()
        result = read_audio_completion(**arguments)
        self.assertEqual(result["status"], "audio_completion_verified")
        self.assertEqual(result["success_key"], self.prefix + "audio_success.json")
        self.assertEqual(arguments["client"].get_object.call_count, 9)
        arguments["client"].put_object.assert_not_called()
        arguments["client"].list_objects_v2.assert_not_called()

    def test_reader_missing_claim_or_marker_is_only_pending(self):
        for missing in ("claim", "marker"):
            with self.subTest(missing=missing):
                arguments, objects = self.make_reader()
                key = (
                    f"runs/{self.run_id}/audio_submission_claim.json"
                    if missing == "claim" else self.prefix + "audio_success.json"
                )
                del objects[key]
                result = read_audio_completion(**arguments)
                self.assertEqual(result["status"], "audio_completion_pending")
                arguments["client"].put_object.assert_not_called()

    def test_reader_access_errors_and_missing_bucket_are_not_pending(self):
        for status, code in ((403, "AccessDenied"), (404, "NoSuchBucket")):
            with self.subTest(code=code):
                arguments, _ = self.make_reader()
                arguments["client"].get_object.side_effect = ClientError(
                    {"Error": {"Code": code},
                     "ResponseMetadata": {"HTTPStatusCode": status}}, "GetObject"
                )
                with self.assertRaises(ClientError):
                    read_audio_completion(**arguments)

    def test_reader_rejects_corrupt_or_missing_required_artifacts(self):
        for scenario in ("hash", "size", "missing"):
            with self.subTest(scenario=scenario):
                arguments, objects = self.make_reader()
                key = self.prefix + "render/episode.mp3"
                if scenario == "hash":
                    objects[key] = b"x" * len(objects[key])
                elif scenario == "size":
                    objects[key] += b"x"
                else:
                    del objects[key]
                error_type = ClientError if scenario == "missing" else ValueError
                with self.assertRaises(error_type):
                    read_audio_completion(**arguments)

    def test_reader_rejects_invalid_json_and_claim_before_attempt_lookup(self):
        for raw in (
            b'{"attempt_id": "a", "attempt_id": "b"}',
            b'{"attempt_id": NaN}',
            b'null',
            b'{"attempt_id": "../another-attempt"}',
        ):
            with self.subTest(raw=raw):
                arguments, objects = self.make_reader()
                objects[f"runs/{self.run_id}/audio_submission_claim.json"] = raw
                with self.assertRaises(ValueError):
                    read_audio_completion(**arguments)
                self.assertEqual(arguments["client"].get_object.call_count, 1)

    def test_reader_checks_evidence_contents_even_with_matching_hashes(self):
        for name, field, value in (
            ("audio_job.json", "bucket", "wrong-bucket"),
            ("verified_script_acceptance.json", "script_sha256", "e" * 64),
            ("job_result.json", "status", "failed"),
            ("render/render_result.json", "completed_dialogue_lines", 1),
        ):
            with self.subTest(name=name):
                arguments, objects = self.make_reader()
                key = self.prefix + name
                content = json.loads(objects[key])
                content[field] = value
                objects[key] = json.dumps(content).encode()
                marker_key = self.prefix + "audio_success.json"
                marker = json.loads(objects[marker_key])
                for artifact in marker["artifacts"]:
                    if artifact["key"] == key:
                        artifact.update(
                            bytes=len(objects[key]),
                            sha256=hashlib.sha256(objects[key]).hexdigest(),
                        )
                objects[marker_key] = json.dumps(marker).encode()
                with self.assertRaises(ValueError):
                    read_audio_completion(**arguments)


if __name__ == "__main__":
    unittest.main()