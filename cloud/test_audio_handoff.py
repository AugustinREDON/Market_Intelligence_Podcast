"""Offline checks for worker verification and atomic job handoff."""

import base64
import fcntl
import hashlib
import io
import json
import os
from pathlib import Path
import shlex
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
from zipfile import ZipFile

from botocore.exceptions import ClientError
from cloud.acceptance import json_sha256
from cloud import audio_handoff as handoff
from cloud import audio_service as service


class AudioHandoffTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        app = root / "app"
        work = root / "work"
        (app / "cloud").mkdir(parents=True)
        work.mkdir()
        (work / "audio_worker.lock").touch()
        bundle = root / "worker.zip"
        with ZipFile(bundle, "w") as archive:
            for name in handoff.WORKER_FILES:
                content = ("trusted " + name).encode()
                (app / name).write_bytes(content)
                archive.writestr(name, content)
        boot = root / "boot_id"
        boot.write_text("23456789-1234-1234-1234-123456789abc")
        self.target = app / "audio_job.json"
        self.target.write_text('{"run_id": "historical-trial"}')
        self.original = self.target.read_bytes()
        self.job = {
            "run_id": "2026-11-06-998",
            "bucket": "offline-test",
            "region": "us-east-2",
            "voices": {},
        }
        self.payload = {
            "run_id": self.job["run_id"],
            "execution_arn": service.EXECUTION_ARN_PREFIX + "handoff-test",
            "instance_id": handoff.INSTANCE_ID,
            "owner_token": "offline-owner",
            "lock_etag": '"offline-etag"',
            "bucket": self.job["bucket"],
            "region": self.job["region"],
            "boot_id": boot.read_text(),
            "handoff_token": "a" * 32,
            "job_sha256": json_sha256(self.job),
            "script_sha256": "b" * 64,
        }
        for name, value in {
            "APP_DIR": app,
            "WORK_ROOT": work,
            "BUNDLE_PATH": bundle,
            "BOOT_PATH": boot,
            "BUNDLE_SHA256": hashlib.sha256(bundle.read_bytes()).hexdigest(),
        }.items():
            self.enterContext(patch.object(handoff, name, value))
        self.versions = self.enterContext(patch.object(
            handoff.importlib.metadata, "version",
            side_effect=lambda name: handoff.EXPECTED_VERSIONS[name],
        ))
        self.enterContext(patch.object(handoff.shutil, "which", return_value="/ffmpeg"))
        self.enterContext(patch.object(
            handoff.pwd, "getpwnam",
            return_value=SimpleNamespace(pw_uid=os.getuid(), pw_gid=os.getgid()),
        ))
        self.chown = self.enterContext(patch.object(handoff.os, "fchown"))
        self.real_loader = handoff.load_verified_job
        self.loader = self.enterContext(patch.object(
            handoff, "load_verified_job", return_value=self.job
        ))
        self.probe = self.enterContext(patch.object(
            handoff, "check_safeguards", return_value={
                "status": "audio_safeguards_verified",
                "probe_token": self.payload["handoff_token"],
                "boot_id": self.payload["boot_id"],
                "generation_requested": False,
            },
        ))
        self.client = Mock()
        self.client.get_object.side_effect = lambda **kwargs: self.owner_response()

    def owner_response(self):
        owner = {
            "schema_version": 1,
            "status": "ownership_held",
            **{key: self.payload[key] for key in (
                "run_id", "execution_arn", "instance_id", "owner_token"
            )},
        }
        return {
            "ETag": self.payload["lock_etag"],
            "Body": io.BytesIO(json.dumps(owner).encode()),
        }

    def perform(self):
        return handoff.handoff_job(
            self.payload, client=self.client, safeguard_source="offline"
        )

    def test_service_start_claim_precedes_launch_and_lock_is_released(self):
        self.target.write_text(json.dumps(self.job))

        def launch(*args, **kwargs):
            self.client.put_object.assert_called_once()
            saved = self.client.put_object.call_args.kwargs
            self.assertEqual(saved["IfNoneMatch"], "*")
            claim = json.loads(saved["Body"])
            self.assertIs(claim["automatic_retry_allowed"], False)
            self.assertIs(claim["generation_requested"], True)

            with (handoff.WORK_ROOT / "audio_worker.lock").open("r+") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)

        with patch.object(
            handoff.subprocess, "run", side_effect=launch
        ) as start:
            result = handoff.start_audio_service(
                self.payload, client=self.client, safeguard_source="offline"
            )

        start.assert_called_once_with(
            ["/usr/bin/systemctl", "start", "mip-audio-job.service"],
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertEqual(
            result["status"], "audio_service_start_acknowledged"
        )
        self.assertIs(result["generation_requested"], True)

    def test_service_start_rejects_changed_job_or_safeguards(self):
        for scenario in ("job", "safeguards"):
            with self.subTest(scenario=scenario):
                self.client.reset_mock()
                self.target.write_text(json.dumps(
                    {} if scenario == "job" else self.job
                ))
                with (
                    patch.dict(self.probe.return_value),
                    patch.object(handoff.subprocess, "run") as start,
                ):
                    if scenario == "safeguards":
                        self.probe.return_value["status"] = "failed"
                    with self.assertRaises(ValueError):
                        handoff.start_audio_service(
                            self.payload,
                            client=self.client,
                            safeguard_source="offline",
                        )
                self.client.put_object.assert_not_called()
                start.assert_not_called()

    def test_service_start_claim_or_launch_errors_never_retry(self):
        self.target.write_text(json.dumps(self.job))

        for scenario in ("claim", "launch"):
            with self.subTest(scenario=scenario):
                self.client.reset_mock()
                self.client.put_object.side_effect = (
                    TimeoutError("uncertain claim")
                    if scenario == "claim" else None
                )
                with patch.object(
                    handoff.subprocess,
                    "run",
                    side_effect=TimeoutError("uncertain launch"),
                ) as start:
                    with self.assertRaises(TimeoutError):
                        handoff.start_audio_service(
                            self.payload,
                            client=self.client,
                            safeguard_source="offline",
                        )
                self.client.put_object.assert_called_once()
                if scenario == "claim":
                    start.assert_not_called()
                else:
                    start.assert_called_once()


    def test_handoff_replaces_only_job_after_verification(self):
        before = {
            name: (handoff.APP_DIR / name).read_bytes()
            for name in handoff.WORKER_FILES
        }
        result = self.perform()
        self.assertEqual(result["status"], "audio_job_handed_off")
        self.assertFalse(result["generation_requested"])
        self.assertEqual(json.loads(self.target.read_text()), self.job)
        self.assertEqual(self.target.stat().st_mode & 0o777, 0o600)
        self.chown.assert_called_once()
        self.assertEqual(self.client.get_object.call_count, 2)
        for call in self.client.get_object.call_args_list:
            self.assertEqual(call.kwargs["IfMatch"], self.payload["lock_etag"])
        for name, content in before.items():
            self.assertEqual((handoff.APP_DIR / name).read_bytes(), content)
        self.client.put_object.assert_not_called()

    def test_handoff_rejects_changed_installation_before_loading_job(self):
        for scenario in ("code", "bundle", "version", "ffmpeg"):
            with self.subTest(scenario=scenario):
                if scenario == "code":
                    target = handoff.APP_DIR / "cloud/audio_job.py"
                    with patch.object(Path, "read_bytes", autospec=True) as read:
                        original_read = Path.open

                        def read_bytes(path):
                            if path == target:
                                return b"changed worker"
                            with original_read(path, "rb") as stream:
                                return stream.read()

                        read.side_effect = read_bytes
                        with self.assertRaises(ValueError):
                            self.perform()
                elif scenario == "bundle":
                    with patch.object(handoff, "BUNDLE_SHA256", "0" * 64):
                        with self.assertRaises(ValueError):
                            self.perform()
                elif scenario == "version":
                    with patch.object(
                        handoff.importlib.metadata, "version", return_value="wrong"
                    ):
                        with self.assertRaises(ValueError):
                            self.perform()
                else:
                    with patch.object(handoff.shutil, "which", return_value=None):
                        with self.assertRaises(ValueError):
                            self.perform()
                self.assertEqual(self.target.read_bytes(), self.original)
                self.loader.assert_not_called()

    def test_handoff_rejects_changed_boot_or_fresh_probe(self):
        for field, value in (
            ("boot_id", "34567890-1234-1234-1234-123456789abc"),
            ("probe_token", "c" * 32),
            ("generation_requested", True),
            ("status", "failed"),
        ):
            with self.subTest(field=field):
                old = self.probe.return_value[field]
                self.probe.return_value[field] = value
                try:
                    with self.assertRaises(ValueError):
                        self.perform()
                finally:
                    self.probe.return_value[field] = old
                self.assertEqual(self.target.read_bytes(), self.original)
        with patch.object(handoff, "BOOT_PATH", handoff.APP_DIR / "audio_job.json"):
            with self.assertRaises(ValueError):
                self.perform()

    def test_handoff_rechecks_ownership_before_replacing_job(self):
        first = self.owner_response()
        second = self.owner_response()
        second["ETag"] = '"changed-etag"'
        self.client.get_object.side_effect = [first, second]
        with self.assertRaises(ValueError):
            self.perform()
        self.assertEqual(self.target.read_bytes(), self.original)

    def test_handoff_respects_worker_lock_and_cleans_failed_replacement(self):
        with (handoff.WORK_ROOT / "audio_worker.lock").open("r+") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaises(BlockingIOError):
                self.perform()
        self.loader.assert_not_called()
        with patch.object(handoff.os, "replace", side_effect=OSError("failed replace")):
            with self.assertRaises(OSError):
                self.perform()
        self.assertEqual(self.target.read_bytes(), self.original)
        self.assertEqual(list(handoff.APP_DIR.glob(".audio_job.*")), [])

    def test_verified_job_requires_matching_job_script_and_no_audio_claim(self):
        missing = ClientError(
            {"Error": {"Code": "404"},
             "ResponseMetadata": {"HTTPStatusCode": 404}}, "HeadObject"
        )
        with (
            patch("cloud.storage.S3RunStorage.read_json", return_value=self.job),
            patch("cloud.audio_job.validate_job", return_value=self.job),
            patch("cloud.acceptance.load_accepted_script", return_value={
                "acceptance": {"script_sha256": self.payload["script_sha256"]}
            }) as accepted,
        ):
            self.client.head_object.side_effect = missing
            self.assertEqual(self.real_loader(self.client, self.payload), self.job)
            accepted.return_value["acceptance"]["script_sha256"] = "c" * 64
            with self.assertRaises(ValueError):
                self.real_loader(self.client, self.payload)
            accepted.return_value["acceptance"]["script_sha256"] = "b" * 64
            with patch.dict(self.payload, {"job_sha256": "0" * 64}):
                with self.assertRaises(ValueError):
                    self.real_loader(self.client, self.payload)
            self.client.head_object.side_effect = None
            with self.assertRaisesRegex(ValueError, "permanent audio claim"):
                self.real_loader(self.client, self.payload)
            denied = ClientError(
                {"Error": {"Code": "AccessDenied"},
                 "ResponseMetadata": {"HTTPStatusCode": 403}}, "HeadObject"
            )
            self.client.head_object.side_effect = denied
            with self.assertRaises(ClientError) as caught:
                self.real_loader(self.client, self.payload)
            self.assertIs(caught.exception, denied)

    def test_handoff_command_preserves_payload_and_compiles(self):
        payload = {**self.payload, "owner_token": "owner ' ; $(false)"}
        parts = shlex.split(handoff.build_audio_handoff_command(payload))
        self.assertEqual(parts[:3], ["/opt/mip/venv/bin/python", "-B", "-c"])
        compile(parts[3], "<offline-handoff>", "exec")
        self.assertEqual(json.loads(base64.b64decode(parts[4])), payload)
        for field, value in (
            ("boot_id", "invalid"),
            ("lock_etag", "*"),
            ("job_sha256", "INVALID"),
            ("run_id", "../wrong"),
            ("instance_id", "i-wrong"),
        ):
            with self.subTest(field=field):
                with self.assertRaises(ValueError):
                    handoff.build_audio_handoff_command({**payload, field: value})

    def test_preparation_binds_handoff_to_verified_job_and_boot(self):
        request = {
            key: self.payload[key] for key in (
                "run_id", "execution_arn", "owner_token", "lock_etag", "bucket"
            )
        }
        request.update(client=self.client, ssm_client=Mock())
        with (
            patch.object(service, "retrieve_audio_safeguard_check", return_value={
                "status": "audio_safeguards_verified",
                "boot_id": self.payload["boot_id"],
                "probe_token": self.payload["handoff_token"],
            }) as retrieve,
            patch.object(service, "prepare_audio_run", return_value={
                "job_sha256": self.payload["job_sha256"],
                "script_sha256": self.payload["script_sha256"],
            }) as prepare,
            patch.object(service.S3RunStorage, "preserve_json", return_value="created") as save,
        ):
            result = service.prepare_audio_handoff(**request)
            retrieve.assert_called_once_with(**request)
            self.assertEqual(result["status"], "audio_handoff_prepared")
            self.assertFalse(result["generation_requested"])
            save.assert_called_once_with(
                self.payload["run_id"], "audio_handoff.json", self.payload
            )
            request["ssm_client"].send_command.assert_not_called()
            retrieve.return_value = {"status": "audio_safeguard_check_pending"}
            prepare.reset_mock()
            save.reset_mock()
            self.assertEqual(
                service.prepare_audio_handoff(**request), retrieve.return_value
            )
            prepare.assert_not_called()
            save.assert_not_called()

    def test_lambda_routes_handoff_preparation(self):
        from cloud.lambda_function import lambda_handler

        arguments = {
            key: self.payload[key] for key in (
                "run_id", "execution_arn", "owner_token", "lock_etag"
            )
        }
        expected = {"status": "audio_handoff_prepared"}
        with patch.object(
            service, "prepare_audio_handoff", return_value=expected
        ) as prepare:
            result = lambda_handler(
                {"action": "prepare_audio_handoff", **arguments}, None
            )
        prepare.assert_called_once_with(**arguments)
        self.assertEqual(result, expected)


if __name__ == "__main__":
    unittest.main()