"""Offline checks for per-run audio job preparation."""
import json
import io
import contextlib
import shlex
import subprocess
import textwrap
import unittest

from pathlib import Path
from botocore.exceptions import ClientError
from unittest.mock import Mock, patch
from datetime import datetime, timezone, timedelta
from cloud.acceptance import json_sha256
from cloud.audio_service import (
    AUDIO_INSTANCE_ID,
    EXECUTION_ARN_PREFIX,
    WORKER_LOCK_KEY,
    acquire_audio_worker,
    prepare_audio_run,
    release_audio_worker,
    start_audio_worker,
    check_audio_worker_connection,
    AUDIO_SAFEGUARD_PROBE,
    build_audio_safeguard_command,
)


class AudioServiceTests(unittest.TestCase):

    def test_worker_lock_blocks_competing_and_repeated_acquisition(self):
        client = Mock()
        saved = {}

        def create_once(**kwargs):
            self.assertEqual(kwargs["IfNoneMatch"], "*")
            self.assertEqual(kwargs["Bucket"], "offline-test")
            self.assertEqual(kwargs["Key"], WORKER_LOCK_KEY)

            if saved:
                raise ClientError(
                    {
                        "Error": {"Code": "PreconditionFailed"},
                        "ResponseMetadata": {"HTTPStatusCode": 412},
                    },
                    "PutObject",
                )

            saved["ownership"] = json.loads(kwargs["Body"])
            return {"ETag": '"offline-lock-etag"'}

        client.put_object.side_effect = create_once
        execution_arn = EXECUTION_ARN_PREFIX + "offline-first"

        first = acquire_audio_worker(
            run_id=self.run_id,
            execution_arn=execution_arn,
            bucket="offline-test",
            client=client,
        )

        self.assertEqual(first["status"], "audio_worker_acquired")
        self.assertEqual(first["instance_id"], AUDIO_INSTANCE_ID)
        self.assertEqual(first["lock_etag"], '"offline-lock-etag"')
        self.assertFalse(first["generation_requested"])

        ownership = saved["ownership"].copy()
        self.assertEqual(ownership["run_id"], self.run_id)
        self.assertEqual(ownership["execution_arn"], execution_arn)
        self.assertEqual(
            ownership["owner_token"],
            first["owner_token"],
        )

        for run_id, owner in (
            ("2026-11-06-999", EXECUTION_ARN_PREFIX + "offline-second"),
            (self.run_id, execution_arn),
        ):
            with self.subTest(owner=owner):
                blocked = acquire_audio_worker(
                    run_id=run_id,
                    execution_arn=owner,
                    bucket="offline-test",
                    client=client,
                )
                self.assertEqual(blocked["status"], "audio_worker_busy")
                self.assertNotIn("owner_token", blocked)
                self.assertNotIn("lock_etag", blocked)
                self.assertEqual(saved["ownership"], ownership)

        self.assertEqual(client.put_object.call_count, 3)
        client.delete_object.assert_not_called()

    def test_worker_lock_errors_do_not_authorize_or_retry(self):
        errors = [
            TimeoutError("Uncertain write response"),
            ClientError(
                {
                    "Error": {"Code": "ConditionalRequestConflict"},
                    "ResponseMetadata": {"HTTPStatusCode": 409},
                },
                "PutObject",
            ),
            ClientError(
                {
                    "Error": {"Code": "AccessDenied"},
                    "ResponseMetadata": {"HTTPStatusCode": 403},
                },
                "PutObject",
            ),
        ]

        for error in errors:
            with self.subTest(error=str(error)):
                client = Mock()
                client.put_object.side_effect = error

                with self.assertRaises(type(error)) as caught:
                    acquire_audio_worker(
                        run_id=self.run_id,
                        execution_arn=(
                            EXECUTION_ARN_PREFIX + "offline-uncertain"
                        ),
                        bucket="offline-test",
                        client=client,
                    )

                self.assertIs(caught.exception, error)
                client.put_object.assert_called_once()
                client.delete_object.assert_not_called()

    def make_release_request(self, state="stopped"):
        ownership = {
            "schema_version": 1,
            "status": "ownership_held",
            "run_id": self.run_id,
            "execution_arn": EXECUTION_ARN_PREFIX + "offline-release",
            "instance_id": AUDIO_INSTANCE_ID,
            "owner_token": "offline-owner-token",
        }
        client = Mock()
        client.get_object.return_value = {
            "ETag": '"offline-lock-etag"',
            "Body": io.BytesIO(json.dumps(ownership).encode("utf-8")),
        }
        client.delete_object.return_value = {
            "ResponseMetadata": {"HTTPStatusCode": 204},
        }

        ec2_client = Mock()
        ec2_client.describe_instances.return_value = {
            "Reservations": [
                {
                    "Instances": [
                        {
                            "InstanceId": AUDIO_INSTANCE_ID,
                            "State": {"Name": state},
                        }
                    ]
                }
            ]
        }

        return {
            "run_id": self.run_id,
            "execution_arn": ownership["execution_arn"],
            "owner_token": ownership["owner_token"],
            "lock_etag": '"offline-lock-etag"',
            "bucket": "offline-test",
            "client": client,
            "ec2_client": ec2_client,
        }

    def test_release_requires_stopped_instance_and_conditional_delete(self):
        request = self.make_release_request()
        result = release_audio_worker(**request)

        self.assertEqual(result["status"], "audio_worker_released")
        self.assertEqual(result["instance_state"], "stopped")
        request["client"].get_object.assert_called_once_with(
            Bucket="offline-test",
            Key=WORKER_LOCK_KEY,
            IfMatch='"offline-lock-etag"',
        )
        request["ec2_client"].describe_instances.assert_called_once_with(
            InstanceIds=[AUDIO_INSTANCE_ID],
        )
        request["client"].delete_object.assert_called_once_with(
            Bucket="offline-test",
            Key=WORKER_LOCK_KEY,
            IfMatch='"offline-lock-etag"',
        )

    def test_release_keeps_lock_until_instance_stops(self):
        for state in ("pending", "running", "stopping"):
            with self.subTest(state=state):
                request = self.make_release_request(state)
                result = release_audio_worker(**request)

                self.assertEqual(
                    result["status"],
                    "audio_worker_not_stopped",
                )
                self.assertEqual(result["instance_state"], state)
                request["client"].delete_object.assert_not_called()
                request["ec2_client"].start_instances.assert_not_called()
                request["ec2_client"].stop_instances.assert_not_called()

    def test_release_rejects_mismatched_ownership_before_ec2_check(self):
        for field, value in (
            ("run_id", "2026-11-06-999"),
            ("execution_arn", EXECUTION_ARN_PREFIX + "another-execution"),
            ("owner_token", "another-token"),
            ("lock_etag", '"another-etag"'),
        ):
            with self.subTest(field=field):
                request = self.make_release_request()
                request[field] = value

                with self.assertRaisesRegex(
                    ValueError,
                    "ownership does not match",
                ):
                    release_audio_worker(**request)

                request["client"].delete_object.assert_not_called()
                request["ec2_client"].describe_instances.assert_not_called()

    def test_release_errors_are_not_retried_or_hidden(self):
        changed_lock = ClientError(
            {
                "Error": {"Code": "PreconditionFailed"},
                "ResponseMetadata": {"HTTPStatusCode": 412},
            },
            "DeleteObject",
        )

        for operation, error in (
            ("get_object", TimeoutError("Uncertain ownership read")),
            ("delete_object", TimeoutError("Uncertain lock deletion")),
            ("delete_object", changed_lock),
        ):
            with self.subTest(operation=operation, error=str(error)):
                request = self.make_release_request()
                method = getattr(request["client"], operation)
                method.side_effect = error

                with self.assertRaises(type(error)) as caught:
                    release_audio_worker(**request)

                self.assertIs(caught.exception, error)
                method.assert_called_once()

                if operation == "get_object":
                    request["client"].delete_object.assert_not_called()
                    request["ec2_client"].describe_instances.assert_not_called()

    def test_lambda_routes_worker_ownership_actions(self):
        from cloud.lambda_function import lambda_handler

        identity = {
            "run_id": self.run_id,
            "execution_arn": EXECUTION_ARN_PREFIX + "offline-routing",
        }

        for action, arguments, status in (
            (
                "acquire_audio_worker",
                identity,
                "audio_worker_acquired",
            ),
            (
                "release_audio_worker",
                {
                    **identity,
                    "owner_token": "offline-owner-token",
                    "lock_etag": '"offline-lock-etag"',
                },
                "audio_worker_released",
            ),
            (
                "check_audio_worker_connection",
                {
                    **identity,
                    "owner_token": "offline-owner-token",
                    "lock_etag": '"offline-lock-etag"',
                },
                "audio_worker_connection_ready",
            ),
            (
                "start_audio_worker",
                {
                    **identity,
                    "owner_token": "offline-owner-token",
                    "lock_etag": '"offline-lock-etag"',
                },
                "audio_instance_start_requested",
            ),
        ):
            with self.subTest(action=action):
                expected = {
                    "status": status,
                    "run_id": self.run_id,
                }

                with patch(
                    f"cloud.audio_service.{action}",
                    return_value=expected,
                ) as service:
                    result = lambda_handler(
                        {"action": action, **arguments},
                        None,
                    )

                service.assert_called_once_with(**arguments)
                self.assertEqual(result, expected)

    def test_start_records_claim_before_requesting_ec2_start(self):
        request = self.make_release_request()

        def start_once(**kwargs):
            self.assertEqual(
                kwargs,
                {"InstanceIds": [AUDIO_INSTANCE_ID]},
            )
            request["client"].put_object.assert_called_once()
            saved = request["client"].put_object.call_args.kwargs
            self.assertEqual(saved["IfNoneMatch"], "*")
            self.assertEqual(
                saved["Key"],
                f"runs/{self.run_id}/audio_instance_start_claim.json",
            )

            claim = json.loads(saved["Body"])
            self.assertEqual(claim["run_id"], self.run_id)
            self.assertEqual(
                claim["owner_token"],
                request["owner_token"],
            )
            self.assertEqual(
                claim["execution_arn"],
                request["execution_arn"],
            )
            self.assertFalse(claim["automatic_retry_allowed"])

            return {
                "StartingInstances": [
                    {
                        "InstanceId": AUDIO_INSTANCE_ID,
                        "PreviousState": {"Name": "stopped"},
                        "CurrentState": {"Name": "pending"},
                    }
                ]
            }

        request["ec2_client"].start_instances.side_effect = start_once
        result = start_audio_worker(**request)

        self.assertEqual(
            result["status"],
            "audio_instance_start_requested",
        )
        self.assertEqual(result["run_id"], self.run_id)
        self.assertFalse(result["generation_requested"])
        request["ec2_client"].start_instances.assert_called_once()

    def test_start_rejects_wrong_owner_before_ec2_check(self):
        request = self.make_release_request()
        request["owner_token"] = "another-owner"

        with self.assertRaisesRegex(
            ValueError,
            "ownership does not match",
        ):
            start_audio_worker(**request)

        request["client"].put_object.assert_not_called()
        request["ec2_client"].describe_instances.assert_not_called()
        request["ec2_client"].start_instances.assert_not_called()

    def test_start_requires_stopped_instance(self):
        for state in ("pending", "running", "stopping"):
            with self.subTest(state=state):
                request = self.make_release_request(state)

                with self.assertRaisesRegex(
                    ValueError,
                    "must be stopped",
                ):
                    start_audio_worker(**request)

                request["client"].put_object.assert_not_called()
                request["ec2_client"].start_instances.assert_not_called()

    def test_start_claim_failure_prevents_ec2_start(self):
        existing_claim = ClientError(
            {
                "Error": {"Code": "PreconditionFailed"},
                "ResponseMetadata": {"HTTPStatusCode": 412},
            },
            "PutObject",
        )

        for error in (
            existing_claim,
            TimeoutError("Uncertain start claim"),
        ):
            with self.subTest(error=str(error)):
                request = self.make_release_request()
                request["client"].put_object.side_effect = error

                with self.assertRaises(type(error)) as caught:
                    start_audio_worker(**request)

                self.assertIs(caught.exception, error)
                request["client"].put_object.assert_called_once()
                request["ec2_client"].start_instances.assert_not_called()

    def test_uncertain_ec2_start_is_not_retried(self):
        for outcome in ("timeout", "unexpected_response"):
            with self.subTest(outcome=outcome):
                request = self.make_release_request()
                start = request["ec2_client"].start_instances

                if outcome == "timeout":
                    start.side_effect = TimeoutError("Uncertain EC2 start")
                    expected_error = TimeoutError
                else:
                    start.return_value = {"StartingInstances": []}
                    expected_error = RuntimeError

                with self.assertRaises(expected_error):
                    start_audio_worker(**request)

                request["client"].put_object.assert_called_once()
                start.assert_called_once_with(
                    InstanceIds=[AUDIO_INSTANCE_ID],
                )
                request["client"].delete_object.assert_not_called()
                request["ec2_client"].stop_instances.assert_not_called()

    def make_connection_request(self, state="running"):
        request = self.make_release_request(state)
        requested_at = datetime.now(timezone.utc) - timedelta(minutes=2)
        claim = {
            "run_id": request["run_id"],
            "execution_arn": request["execution_arn"],
            "instance_id": AUDIO_INSTANCE_ID,
            "owner_token": request["owner_token"],
            "requested_at": requested_at.isoformat(),
            "automatic_retry_allowed": False,
        }

        ownership_response = request["client"].get_object.return_value
        request["client"].get_object.side_effect = [
            ownership_response,
            {
                "Body": io.BytesIO(
                    json.dumps(claim).encode("utf-8")
                ),
            },
        ]

        request["ssm_client"] = Mock()
        request["ssm_client"].describe_instance_information.return_value = {
            "InstanceInformationList": [
                {
                    "InstanceId": AUDIO_INSTANCE_ID,
                    "PingStatus": "Online",
                    "LastPingDateTime": (
                        requested_at + timedelta(minutes=1)
                    ),
                }
            ],
        }
        return request, requested_at

    def test_connection_requires_fresh_online_ssm(self):
        for scenario in (
            "fresh",
            "stale",
            "offline",
            "missing",
            "future",
            "naive",
        ):
            with self.subTest(scenario=scenario):
                request, requested_at = self.make_connection_request()
                ssm = request["ssm_client"]
                response = (
                    ssm.describe_instance_information.return_value
                )
                node = response["InstanceInformationList"][0]

                if scenario == "stale":
                    node["LastPingDateTime"] = requested_at
                elif scenario == "offline":
                    node["PingStatus"] = "ConnectionLost"
                elif scenario == "missing":
                    response["InstanceInformationList"] = []
                elif scenario == "future":
                    node["LastPingDateTime"] = (
                        datetime.now(timezone.utc)
                        + timedelta(minutes=5)
                    )
                elif scenario == "naive":
                    node["LastPingDateTime"] = datetime.now()

                result = check_audio_worker_connection(**request)

                expected_status = (
                    "audio_worker_connection_ready"
                    if scenario == "fresh"
                    else "audio_worker_connection_pending"
                )
                self.assertEqual(result["status"], expected_status)
                self.assertFalse(result["generation_requested"])
                ssm.describe_instance_information.assert_called_once_with(
                    Filters=[
                        {
                            "Key": "InstanceIds",
                            "Values": [AUDIO_INSTANCE_ID],
                        }
                    ],
                )
                ssm.send_command.assert_not_called()
                request["ec2_client"].start_instances.assert_not_called()
                request["ec2_client"].stop_instances.assert_not_called()
                request["client"].put_object.assert_not_called()
                request["client"].delete_object.assert_not_called()

    def test_connection_checks_instance_before_ssm(self):
        for state in ("pending", "stopped", "stopping", "terminated"):
            with self.subTest(state=state):
                request, _ = self.make_connection_request(state)

                if state == "pending":
                    result = check_audio_worker_connection(**request)
                    self.assertEqual(
                        result["status"],
                        "audio_worker_connection_pending",
                    )
                    self.assertEqual(result["reason"], "instance_pending")
                else:
                    with self.assertRaisesRegex(
                        ValueError,
                        "not starting or running",
                    ):
                        check_audio_worker_connection(**request)

                request["ssm_client"].assert_not_called()
                request["ssm_client"].describe_instance_information.assert_not_called()
                request["ssm_client"].send_command.assert_not_called()

    def test_connection_rejects_wrong_owner_before_ec2_or_ssm(self):
        request, _ = self.make_connection_request()
        request["owner_token"] = "another-owner"

        with self.assertRaisesRegex(
            ValueError,
            "ownership does not match",
        ):
            check_audio_worker_connection(**request)

        request["client"].get_object.assert_called_once()
        request["ec2_client"].describe_instances.assert_not_called()
        request["ssm_client"].describe_instance_information.assert_not_called()

    def test_connection_rejects_start_claim_for_another_execution(self):
        request, requested_at = self.make_connection_request()
        ownership_response = (
            request["client"].get_object.side_effect
        )
        ownership_response = next(ownership_response)

        request["client"].get_object.side_effect = [
            ownership_response,
            {
                "Body": io.BytesIO(
                    json.dumps(
                        {
                            "run_id": request["run_id"],
                            "execution_arn": (
                                EXECUTION_ARN_PREFIX + "another-execution"
                            ),
                            "instance_id": AUDIO_INSTANCE_ID,
                            "owner_token": request["owner_token"],
                            "requested_at": requested_at.isoformat(),
                            "automatic_retry_allowed": False,
                        }
                    ).encode("utf-8")
                ),
            },
        ]

        with self.assertRaisesRegex(
            ValueError,
            "Start claim does not match",
        ):
            check_audio_worker_connection(**request)

        request["ec2_client"].describe_instances.assert_not_called()
        request["ssm_client"].describe_instance_information.assert_not_called()

    def test_safeguard_command_preserves_script_and_validates_token(self):
        token = "a" * 32
        command = build_audio_safeguard_command(probe_token=token)

        self.assertEqual(
            shlex.split(command),
            [
                "/opt/mip/venv/bin/python",
                "-c",
                AUDIO_SAFEGUARD_PROBE,
                token,
            ],
        )
        compile(AUDIO_SAFEGUARD_PROBE, "<offline-probe>", "exec")

        for invalid in (
            None,
            "",
            "a" * 31,
            "A" * 32,
            ";" * 32,
            "a" * 32 + "\n",
        ):
            with self.subTest(token=invalid):
                with self.assertRaises(ValueError):
                    build_audio_safeguard_command(
                        probe_token=invalid,
                    )

    def test_safeguard_probe_checks_installed_units_and_timer(self):
        # Use the installation file as the service configuration fixture.
        source = (
            Path(__file__).resolve().parent / "audio_user_data.yaml"
        ).read_text()

        units = {}
        for unit in (
            "mip-max-runtime.service",
            "mip-max-runtime.timer",
            "mip-audio-job.service",
        ):
            section = source.split(
                f"  - path: /etc/systemd/system/{unit}\n",
                1,
            )[1]
            content = section.split("    content: |\n", 1)[1]
            lines = []
            for line in content.splitlines():
                if line.strip() and not line.startswith("      "):
                    break
                lines.append(line)
            units[unit] = textwrap.dedent("\n".join(lines))

        for scenario in (
            "healthy",
            "timer_inactive",
            "timer_expired",
            "timer_too_late",
            "audio_running",
            "audio_enabled",
            "reload_needed",
            "changed_unit",
        ):
            with self.subTest(scenario=scenario):
                properties = {
                    ("mip-max-runtime.timer", "ActiveState"): "active",
                    ("mip-max-runtime.timer", "SubState"): "waiting",
                    (
                        "mip-max-runtime.timer",
                        "NextElapseUSecMonotonic",
                    ): "5400000000",
                    ("mip-audio-job.service", "UnitFileState"): "static",
                    ("mip-audio-job.service", "ActiveState"): "inactive",
                    ("mip-max-runtime.service", "ActiveState"): "inactive",
                }

                if scenario == "timer_inactive":
                    properties[
                        ("mip-max-runtime.timer", "ActiveState")
                    ] = "inactive"
                elif scenario == "timer_expired":
                    properties[
                        (
                            "mip-max-runtime.timer",
                            "NextElapseUSecMonotonic",
                        )
                    ] = "1000000"
                elif scenario == "timer_too_late":
                    properties[
                        (
                            "mip-max-runtime.timer",
                            "NextElapseUSecMonotonic",
                        )
                    ] = "7200000000"
                elif scenario == "audio_running":
                    properties[
                        ("mip-audio-job.service", "ActiveState")
                    ] = "active"
                elif scenario == "audio_enabled":
                    properties[
                        ("mip-audio-job.service", "UnitFileState")
                    ] = "enabled"

                def fake_systemctl(command, **kwargs):
                    self.assertEqual(command[0], "/usr/bin/systemctl")
                    self.assertTrue(kwargs["check"])
                    self.assertEqual(kwargs["timeout"], 10)
                    operation, unit = command[1:3]

                    if operation == "cat":
                        output = units[unit]
                        if (
                            scenario == "changed_unit"
                            and unit == "mip-audio-job.service"
                        ):
                            output = output.replace(
                                "Restart=no",
                                "Restart=always",
                            )
                    elif operation == "is-enabled":
                        output = "enabled"
                    elif operation == "show":
                        property_name = command[3].split("=", 1)[1]
                        if property_name == "LoadState":
                            output = "loaded"
                        elif property_name == "NeedDaemonReload":
                            output = (
                                "yes"
                                if scenario == "reload_needed"
                                else "no"
                            )
                        else:
                            output = properties[(unit, property_name)]
                    else:
                        self.fail(f"Unexpected command: {command}")

                    return subprocess.CompletedProcess(
                        command,
                        0,
                        stdout=output + "\n",
                        stderr="",
                    )

                def fake_read_text(path, *args, **kwargs):
                    if str(path) == "/proc/uptime":
                        return "120.0 100.0\n"
                    if str(path) == "/proc/sys/kernel/random/boot_id":
                        return "offline-boot-id\n"
                    self.fail(f"Unexpected file read: {path}")

                output = io.StringIO()
                with (
                    patch(
                        "subprocess.run",
                        side_effect=fake_systemctl,
                    ),
                    patch.object(
                        Path,
                        "read_text",
                        new=fake_read_text,
                    ),
                    patch("sys.argv", ["probe", "a" * 32]),
                    contextlib.redirect_stdout(output),
                ):
                    if scenario == "healthy":
                        exec(
                            compile(
                                AUDIO_SAFEGUARD_PROBE,
                                "<offline-probe>",
                                "exec",
                            ),
                            {},
                        )
                    else:
                        with self.assertRaises(RuntimeError):
                            exec(
                                compile(
                                    AUDIO_SAFEGUARD_PROBE,
                                    "<offline-probe>",
                                    "exec",
                                ),
                                {},
                            )

                if scenario == "healthy":
                    result = json.loads(output.getvalue())
                    self.assertEqual(
                        result["status"],
                        "audio_safeguards_verified",
                    )
                    self.assertEqual(result["probe_token"], "a" * 32)
                    self.assertEqual(
                        result["boot_id"],
                        "offline-boot-id",
                    )
                    self.assertFalse(result["generation_requested"])
                else:
                    self.assertEqual(output.getvalue(), "")

    def test_lambda_routes_audio_preparation(self):
        from cloud.lambda_function import lambda_handler

        expected = {
            "status": "audio_job_prepared",
            "run_id": self.run_id,
            "generation_requested": False,
        }

        with patch(
            "cloud.audio_service.prepare_audio_run",
            return_value=expected,
        ) as prepare:
            result = lambda_handler(
                {
                    "action": "prepare_audio",
                    "run_id": self.run_id,
                },
                None,
            )

        prepare.assert_called_once_with(run_id=self.run_id)
        self.assertEqual(result, expected)

    def setUp(self):
        self.run_id = "2026-11-06-998"
        self.storage = Mock()
        self.storage.bucket = "offline-test"
        self.storage.preserve_json.return_value = "created"

        loader = patch("cloud.audio_service.load_accepted_script")
        self.loader = loader.start()
        self.addCleanup(loader.stop)
        self.loader.return_value = {
            "acceptance": {"script_sha256": "a" * 64},
        }

    def test_preparation_binds_job_to_verified_run(self):
        result = prepare_audio_run(
            run_id=self.run_id,
            storage=self.storage,
        )

        self.loader.assert_called_once_with(
            run_id=self.run_id,
            storage=self.storage,
        )
        self.storage.preserve_json.assert_called_once()
        saved_run, filename, job = (
            self.storage.preserve_json.call_args.args
        )

        self.assertEqual(saved_run, self.run_id)
        self.assertEqual(filename, "audio_job.json")
        self.assertEqual(job["run_id"], self.run_id)
        self.assertEqual(job["bucket"], "offline-test")
        self.assertEqual(job["region"], "us-east-2")
        self.assertEqual(set(job["voices"]), {"HOST_A", "HOST_B"})

        for speaker, reference in job["voices"].items():
            self.assertEqual(
                reference["key"],
                f"assets/voices/v1/{speaker.lower()}.wav",
            )
            self.assertRegex(reference["sha256"], r"^[0-9a-f]{64}$")

        self.assertEqual(result["status"], "audio_job_prepared")
        self.assertEqual(result["run_id"], self.run_id)
        self.assertEqual(
            result["job_key"],
            f"runs/{self.run_id}/audio_job.json",
        )
        self.assertEqual(result["job_sha256"], json_sha256(job))
        self.assertEqual(result["script_sha256"], "a" * 64)
        self.assertFalse(result["generation_requested"])

    def test_acceptance_failure_prevents_job_write(self):
        self.loader.side_effect = ValueError("Acceptance mismatch")

        with self.assertRaisesRegex(ValueError, "Acceptance mismatch"):
            prepare_audio_run(
                run_id=self.run_id,
                storage=self.storage,
            )

        self.storage.preserve_json.assert_not_called()

    def test_storage_conflict_is_not_retried_or_hidden(self):
        self.storage.preserve_json.side_effect = ValueError(
            "Existing S3 artifact differs"
        )

        with self.assertRaisesRegex(ValueError, "artifact differs"):
            prepare_audio_run(
                run_id=self.run_id,
                storage=self.storage,
            )

        self.storage.preserve_json.assert_called_once()


if __name__ == "__main__":
    unittest.main()