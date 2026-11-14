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
from cloud import audio_service as service
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
    submit_audio_safeguard_check,
    validate_audio_safeguard_output,
    build_audio_safeguard_request,
    retrieve_audio_safeguard_check,
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

    def make_stop_request(self, state="running"):
        request = self.make_release_request(state)
        client = request["client"]
        owner = json.loads(client.get_object.return_value["Body"].getvalue())
        now = datetime.now(timezone.utc).isoformat()
        claim = {
            "run_id": self.run_id,
            "execution_arn": request["execution_arn"],
            "instance_id": AUDIO_INSTANCE_ID,
            "owner_token": request["owner_token"],
            "requested_at": now,
            "automatic_retry_allowed": False,
        }
        prefix = f"runs/{self.run_id}/"
        objects = {
            WORKER_LOCK_KEY: json.dumps(owner).encode(),
            prefix + "audio_instance_start_claim.json": json.dumps(claim).encode(),
            prefix + "audio_instance_start_receipt.json": json.dumps({
                **claim,
                "status": "audio_instance_start_acknowledged",
                "start_claim_key": prefix + "audio_instance_start_claim.json",
                "acknowledged_at": now,
                "previous_state": "stopped",
                "current_state": "pending",
            }).encode(),
        }

        def read(**kwargs):
            key = kwargs["Key"]
            if key not in objects:
                raise ClientError(
                    {"Error": {"Code": "NoSuchKey"},
                     "ResponseMetadata": {"HTTPStatusCode": 404}},
                    "GetObject",
                )
            return {
                "ETag": request["lock_etag"],
                "Body": io.BytesIO(objects[key]),
            }

        def put(**kwargs):
            self.assertEqual(kwargs["IfNoneMatch"], "*")
            if kwargs["Key"] in objects:
                raise ClientError(
                    {"Error": {"Code": "PreconditionFailed"},
                     "ResponseMetadata": {"HTTPStatusCode": 412}},
                    "PutObject",
                )
            objects[kwargs["Key"]] = kwargs["Body"]
            return {}

        client.get_object.side_effect = read
        client.put_object.side_effect = put
        request["ec2_client"].stop_instances.return_value = {
            "StoppingInstances": [{
                "InstanceId": AUDIO_INSTANCE_ID,
                "PreviousState": {"Name": "running"},
                "CurrentState": {"Name": "stopping"},
            }]
        }
        return request, objects

    def test_stop_claim_precedes_one_request_and_receipt_is_saved(self):
        request, objects = self.make_stop_request()
        ec2 = request["ec2_client"]
        response = ec2.stop_instances.return_value

        def stop(**kwargs):
            key = f"runs/{self.run_id}/audio_instance_stop_claim.json"
            self.assertIn(key, objects)
            self.assertIs(
                json.loads(objects[key])["automatic_retry_allowed"], False
            )
            return response

        ec2.stop_instances.side_effect = stop
        result = service.submit_audio_worker_stop(**request)
        self.assertEqual(result["status"], "audio_worker_stop_requested")
        self.assertIn(result["stop_receipt_key"], objects)
        ec2.stop_instances.assert_called_once_with(
            InstanceIds=[AUDIO_INSTANCE_ID]
        )
        request["client"].delete_object.assert_not_called()
        ec2.start_instances.assert_not_called()

    def test_stop_does_not_submit_for_nonrunning_states(self):
        for state in ("pending", "stopping", "stopped"):
            with self.subTest(state=state):
                request, objects = self.make_stop_request(state)
                result = service.submit_audio_worker_stop(**request)
                self.assertEqual(
                    result["status"],
                    "audio_worker_shutdown_confirmed"
                    if state == "stopped"
                    else "audio_worker_shutdown_pending",
                )
                self.assertIs(result["stop_requested"], False)
                request["ec2_client"].stop_instances.assert_not_called()
                request["client"].put_object.assert_not_called()
                request["client"].delete_object.assert_not_called()

    def test_stop_requires_owned_matching_startup_evidence(self):
        for scenario in ("owner", "missing", "changed", "timing"):
            with self.subTest(scenario=scenario):
                request, objects = self.make_stop_request()
                key = f"runs/{self.run_id}/audio_instance_start_receipt.json"
                if scenario == "owner":
                    request["owner_token"] = "wrong"
                elif scenario == "missing":
                    del objects[key]
                else:
                    receipt = json.loads(objects[key])
                    if scenario == "changed":
                        receipt["owner_token"] = "wrong"
                    else:
                        receipt["acknowledged_at"] = "2020-01-01T00:00:00+00:00"
                    objects[key] = json.dumps(receipt).encode()

                error_type = ClientError if scenario == "missing" else ValueError
                with self.assertRaises(error_type):
                    service.submit_audio_worker_stop(**request)
                self.assertEqual(request["ec2_client"].mock_calls, [])
                request["client"].put_object.assert_not_called()

    def test_uncertain_stop_never_authorizes_resubmission(self):
        for scenario in ("claim", "send", "response", "receipt"):
            with self.subTest(scenario=scenario):
                request, objects = self.make_stop_request()
                prefix = f"runs/{self.run_id}/"
                claim_key = prefix + "audio_instance_stop_claim.json"
                receipt_key = prefix + "audio_instance_stop_receipt.json"
                original_put = request["client"].put_object.side_effect

                def put(**kwargs):
                    original_put(**kwargs)
                    fail_key = claim_key if scenario == "claim" else receipt_key
                    if (
                        scenario in {"claim", "receipt"}
                        and kwargs["Key"] == fail_key
                    ):
                        raise TimeoutError("Uncertain stop evidence write")
                    return {}

                request["client"].put_object.side_effect = put
                ec2 = request["ec2_client"]
                if scenario == "send":
                    ec2.stop_instances.side_effect = TimeoutError("Uncertain stop")
                elif scenario == "response":
                    ec2.stop_instances.return_value = {}

                expected_error = RuntimeError if scenario == "response" else TimeoutError
                with self.assertRaises(expected_error):
                    service.submit_audio_worker_stop(**request)

                self.assertIn(claim_key, objects)
                self.assertEqual(
                    ec2.stop_instances.call_count,
                    0 if scenario == "claim" else 1,
                )
                ec2.stop_instances.reset_mock()
                with self.assertRaises(ClientError):
                    service.submit_audio_worker_stop(**request)
                ec2.stop_instances.assert_not_called()
                request["client"].delete_object.assert_not_called()

    def test_lambda_routes_worker_stop(self):
        from cloud.lambda_function import lambda_handler

        request, objects = self.make_stop_request()
        arguments = {
            key: request[key] for key in (
                "run_id", "execution_arn", "owner_token", "lock_etag"
            )
        }
        with patch.object(
            service, "submit_audio_worker_stop",
            return_value={"status": "audio_worker_stop_requested"},
        ) as operation:
            result = lambda_handler(
                {"action": "submit_audio_worker_stop", **arguments}, None
            )

        self.assertEqual(result["status"], "audio_worker_stop_requested")
        operation.assert_called_once_with(**arguments)

    def test_shutdown_monitor_bounds_waiting_without_mutations(self):
        now = datetime(2026, 11, 12, 4, 0, tzinfo=timezone.utc)
        for state in ("pending", "running", "stopping", "stopped"):
            for elapsed in (599, 600, 601):
                with self.subTest(state=state, elapsed=elapsed):
                    request = self.make_release_request(state)
                    started = (
                        now - timedelta(seconds=elapsed)
                    ).isoformat()

                    with patch.object(
                        service, "datetime", wraps=datetime
                    ) as clock:
                        clock.now.return_value = now
                        result = service.monitor_audio_worker_shutdown(
                            **request,
                            shutdown_wait_started_at=started,
                        )

                    expected = "audio_worker_shutdown_pending"
                    if state == "stopped":
                        expected = "audio_worker_shutdown_confirmed"
                    elif elapsed >= 600:
                        expected = "audio_worker_shutdown_deadline_exceeded"

                    self.assertEqual(result["status"], expected)
                    self.assertEqual(result["elapsed_seconds"], elapsed)
                    self.assertEqual(
                        result["shutdown_wait_started_at"], started
                    )
                    self.assertIs(result["generation_requested"], False)
                    self.assertEqual(
                        len(request["ec2_client"].mock_calls), 1
                    )
                    request["client"].put_object.assert_not_called()
                    request["client"].delete_object.assert_not_called()

    def test_shutdown_monitor_rejects_invalid_wait_before_aws(self):
        now = datetime(2026, 11, 12, 4, 0, tzinfo=timezone.utc)
        for timestamp in (
            None,
            "",
            "invalid",
            "2026-11-12T03:59:00",
            (now + timedelta(seconds=1)).isoformat(),
        ):
            with self.subTest(timestamp=timestamp):
                request = self.make_release_request()
                with patch.object(
                    service, "datetime", wraps=datetime
                ) as clock:
                    clock.now.return_value = now
                    with self.assertRaises(ValueError):
                        service.monitor_audio_worker_shutdown(
                            **request,
                            shutdown_wait_started_at=timestamp,
                        )

                self.assertEqual(request["client"].mock_calls, [])
                self.assertEqual(request["ec2_client"].mock_calls, [])

    def test_shutdown_monitor_checks_ownership_even_after_deadline(self):
        request = self.make_release_request()
        request["owner_token"] = "wrong"
        started = (
            datetime.now(timezone.utc) - timedelta(minutes=11)
        ).isoformat()

        with self.assertRaises(ValueError):
            service.monitor_audio_worker_shutdown(
                **request,
                shutdown_wait_started_at=started,
            )

        self.assertEqual(request["ec2_client"].mock_calls, [])
        request["client"].delete_object.assert_not_called()

    def test_shutdown_monitor_propagates_ec2_error_without_retry(self):
        request = self.make_release_request()
        error = TimeoutError("Shutdown observation uncertain")
        request["ec2_client"].describe_instances.side_effect = error

        with self.assertRaises(TimeoutError) as caught:
            service.monitor_audio_worker_shutdown(
                **request,
                shutdown_wait_started_at=(
                    datetime.now(timezone.utc) - timedelta(seconds=1)
                ).isoformat(),
            )

        self.assertIs(caught.exception, error)
        request["ec2_client"].describe_instances.assert_called_once()
        request["ec2_client"].stop_instances.assert_not_called()
        request["client"].delete_object.assert_not_called()

    def test_lambda_routes_shutdown_monitor(self):
        from cloud.lambda_function import lambda_handler

        request = self.make_release_request()
        arguments = {
            key: request[key] for key in (
                "run_id", "execution_arn", "owner_token", "lock_etag"
            )
        }
        arguments["shutdown_wait_started_at"] = (
            datetime.now(timezone.utc).isoformat()
        )
        with patch.object(
            service,
            "monitor_audio_worker_shutdown",
            return_value={
                "status": "audio_worker_shutdown_deadline_exceeded"
            },
        ) as operation:
            result = lambda_handler(
                {"action": "monitor_audio_worker_shutdown", **arguments},
                None,
            )

        self.assertEqual(
            result["status"], "audio_worker_shutdown_deadline_exceeded"
        )
        operation.assert_called_once_with(**arguments)


    def test_shutdown_check_only_confirms_stopped_and_never_mutates(self):
        for state in ("pending", "running", "stopping", "stopped"):
            with self.subTest(state=state):
                request = self.make_release_request(state)
                result = service.check_audio_worker_shutdown(**request)

                self.assertEqual(
                    result["status"],
                    "audio_worker_shutdown_confirmed"
                    if state == "stopped"
                    else "audio_worker_shutdown_pending",
                )
                self.assertEqual(result["instance_state"], state)
                self.assertIs(result["generation_requested"], False)
                request["ec2_client"].describe_instances.assert_called_once_with(
                    InstanceIds=[AUDIO_INSTANCE_ID],
                )
                self.assertEqual(
                    len(request["ec2_client"].mock_calls), 1
                )
                request["client"].put_object.assert_not_called()
                request["client"].delete_object.assert_not_called()

    def test_shutdown_check_rejects_wrong_owner_before_ec2(self):
        request = self.make_release_request()
        request["owner_token"] = "wrong"
        with self.assertRaises(ValueError):
            service.check_audio_worker_shutdown(**request)

        self.assertEqual(request["ec2_client"].mock_calls, [])
        request["client"].delete_object.assert_not_called()

    def test_shutdown_check_rejects_bad_instance_and_propagates_read_errors(self):
        for scenario in ("missing", "wrong", "terminated", "access"):
            with self.subTest(scenario=scenario):
                request = self.make_release_request()
                ec2 = request["ec2_client"]
                instance = ec2.describe_instances.return_value[
                    "Reservations"
                ][0]["Instances"][0]

                if scenario == "missing":
                    ec2.describe_instances.return_value = {
                        "Reservations": []
                    }
                elif scenario == "wrong":
                    instance["InstanceId"] = "i-wrong"
                elif scenario == "terminated":
                    instance["State"]["Name"] = "terminated"
                else:
                    ec2.describe_instances.side_effect = ClientError(
                        {"Error": {"Code": "UnauthorizedOperation"}},
                        "DescribeInstances",
                    )

                error_type = ClientError if scenario == "access" else ValueError
                with self.assertRaises(error_type):
                    service.check_audio_worker_shutdown(**request)

                ec2.describe_instances.assert_called_once()
                ec2.stop_instances.assert_not_called()
                request["client"].delete_object.assert_not_called()

    def test_lambda_routes_shutdown_check(self):
        from cloud.lambda_function import lambda_handler

        request = self.make_release_request()
        arguments = {
            key: request[key] for key in (
                "run_id", "execution_arn", "owner_token", "lock_etag"
            )
        }
        with patch.object(
            service,
            "check_audio_worker_shutdown",
            return_value={"status": "audio_worker_shutdown_confirmed"},
        ) as operation:
            result = lambda_handler(
                {"action": "check_audio_worker_shutdown", **arguments},
                None,
            )

        self.assertEqual(result["status"], "audio_worker_shutdown_confirmed")
        operation.assert_called_once_with(**arguments)


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
                "submit_audio_safeguard_check",
                {
                    **identity,
                    "owner_token": "offline-owner-token",
                    "lock_etag": '"offline-lock-etag"',
                },
                "audio_safeguard_check_submitted",
            ),
            (
                "retrieve_audio_safeguard_check",
                {
                    **identity,
                    "owner_token": "offline-owner-token",
                    "lock_etag": '"offline-lock-etag"',
                },
                "audio_safeguards_verified",
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

        writes = request["client"].put_object.call_args_list
        self.assertEqual(len(writes), 2)
        claim = json.loads(writes[0].kwargs["Body"])
        saved = writes[1].kwargs
        receipt = json.loads(saved["Body"])

        self.assertEqual(saved["IfNoneMatch"], "*")
        self.assertEqual(saved["Bucket"], request["bucket"])
        self.assertEqual(
            saved["Key"],
            f"runs/{self.run_id}/audio_instance_start_receipt.json",
        )
        self.assertEqual(result["start_receipt_key"], saved["Key"])
        self.assertEqual(
            receipt,
            {
                **claim,
                "status": "audio_instance_start_acknowledged",
                "start_claim_key": result["start_claim_key"],
                "acknowledged_at": receipt["acknowledged_at"],
                "previous_state": "stopped",
                "current_state": "pending",
            },
        )
        self.assertIs(receipt["automatic_retry_allowed"], False)
        self.assertLessEqual(
            datetime.fromisoformat(claim["requested_at"]),
            datetime.fromisoformat(receipt["acknowledged_at"]),
        )

    def test_start_receipt_failure_preserves_claim_without_retrying(self):
        for receipt_saved in (False, True):
            with self.subTest(receipt_saved=receipt_saved):
                request = self.make_release_request()
                objects = {}
                claim_key = (
                    f"runs/{self.run_id}/audio_instance_start_claim.json"
                )
                receipt_key = (
                    f"runs/{self.run_id}/audio_instance_start_receipt.json"
                )

                def put(**kwargs):
                    self.assertEqual(kwargs["IfNoneMatch"], "*")
                    key = kwargs["Key"]
                    if key == receipt_key:
                        if receipt_saved:
                            objects[key] = kwargs["Body"]
                        raise TimeoutError("Uncertain startup receipt write")
                    objects[key] = kwargs["Body"]
                    return {}

                request["client"].put_object.side_effect = put
                request["ec2_client"].start_instances.return_value = {
                    "StartingInstances": [{
                        "InstanceId": AUDIO_INSTANCE_ID,
                        "PreviousState": {"Name": "stopped"},
                        "CurrentState": {"Name": "pending"},
                    }]
                }

                with self.assertRaises(TimeoutError):
                    start_audio_worker(**request)

                self.assertIn(claim_key, objects)
                self.assertEqual(receipt_key in objects, receipt_saved)
                request["ec2_client"].start_instances.assert_called_once_with(
                    InstanceIds=[AUDIO_INSTANCE_ID],
                )
                self.assertEqual(request["client"].put_object.call_count, 2)
                request["client"].delete_object.assert_not_called()
                request["ec2_client"].stop_instances.assert_not_called()

    def test_malformed_start_response_never_creates_receipt(self):
        for response in (
            None,
            {},
            {"StartingInstances": None},
            {"StartingInstances": [None]},
            {
                "StartingInstances": [{
                    "InstanceId": AUDIO_INSTANCE_ID,
                    "PreviousState": None,
                    "CurrentState": {"Name": "pending"},
                }]
            },
            {
                "StartingInstances": [{
                    "InstanceId": "i-wrong",
                    "PreviousState": {"Name": "stopped"},
                    "CurrentState": {"Name": "pending"},
                }]
            },
        ):
            with self.subTest(response=response):
                request = self.make_release_request()
                request["ec2_client"].start_instances.return_value = response

                with self.assertRaises(RuntimeError):
                    start_audio_worker(**request)

                # Only the pre-request claim was written.
                request["client"].put_object.assert_called_once()
                request["ec2_client"].start_instances.assert_called_once()
                request["client"].delete_object.assert_not_called()


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
                    ): "1h",
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

    def test_safeguard_duration_parser(self):
        import ast

        namespace = {}
        tree = ast.parse(AUDIO_SAFEGUARD_PROBE)
        functions = ast.Module(
            body=[
                node
                for node in tree.body
                if isinstance(node, ast.FunctionDef)
                and node.name in {"require", "duration_microseconds"}
            ],
            type_ignores=[],
        )
        exec(compile(functions, "<duration-parser-test>", "exec"), namespace)
        parse = namespace["duration_microseconds"]

        for value, expected in (
            ("1h", 3_600_000_000),
            ("1h 1s", 3_601_000_000),
            ("59min 59.5s", 3_599_500_000),
            ("1ms 2us", 1_002),
            ("3600000000", 3_600_000_000),
        ):
            with self.subTest(value=value):
                self.assertEqual(parse(value), expected)

        for value in ("", "infinity", "-1s", "1h garbage", "0.1us"):
            with self.subTest(value=value):
                with self.assertRaises(RuntimeError):
                    parse(value)

    def test_safeguard_output_accepts_expected_probe(self):
        probe = {
            "status": "audio_safeguards_verified",
            "probe_token": "a" * 32,
            "boot_id": "12345678-1234-1234-1234-123456789abc",
            "uptime_seconds": 120.25,
            "shutdown_deadline_monotonic_us": 3_600_000_000,
            "generation_requested": False,
        }
        self.assertEqual(
            validate_audio_safeguard_output(
                output=json.dumps(probe) + "\n",
                probe_token="a" * 32,
            ),
            probe,
        )

    def test_safeguard_output_rejects_invalid_evidence(self):
        probe = {
            "status": "audio_safeguards_verified",
            "probe_token": "a" * 32,
            "boot_id": "12345678-1234-1234-1234-123456789abc",
            "uptime_seconds": 120.25,
            "shutdown_deadline_monotonic_us": 3_600_000_000,
            "generation_requested": False,
        }
        mutations = [
            {"status": "audio_safeguard_check_submitted"},
            {"probe_token": "b" * 32},
            {"generation_requested": True},
            {"generation_requested": 0},
            {"boot_id": "offline-boot-id"},
            {"boot_id": probe["boot_id"].upper()},
            {"uptime_seconds": True},
            {"uptime_seconds": -1},
            {"uptime_seconds": float("nan")},
            {"uptime_seconds": float("inf")},
            {"uptime_seconds": 10 ** 400},
            {"uptime_seconds": 3601},
            {"shutdown_deadline_monotonic_us": True},
            {"shutdown_deadline_monotonic_us": 3_600_000_000.0},
            {"shutdown_deadline_monotonic_us": 120_250_000},
            {"shutdown_deadline_monotonic_us": 3_601_000_001},
            {"unexpected": "field"},
        ]
        outputs = [
            json.dumps({**probe, **change})
            for change in mutations
        ]
        outputs.extend([
            "",
            "not JSON",
            "[]",
            "null",
            "{}",
            " " * 4097,
            json.dumps(probe).replace(
                '"generation_requested": false',
                '"generation_requested": false, '
                '"generation_requested": false',
            ),
        ])

        for output in outputs:
            with self.subTest(output=output):
                with self.assertRaises(ValueError):
                    validate_audio_safeguard_output(
                        output=output,
                        probe_token="a" * 32,
                    )

        for token in (None, "", "A" * 32, "a" * 31):
            with self.subTest(token=token):
                with self.assertRaises(ValueError):
                    validate_audio_safeguard_output(
                        output=json.dumps(probe),
                        probe_token=token,
                    )

    def make_safeguard_retrieval_request(self):
        request = self.make_release_request("running")
        request.pop("ec2_client")
        client = request["client"]
        ownership = json.load(client.get_object.return_value["Body"])
        now = datetime.now(timezone.utc)
        token = "a" * 32
        identity = {
            key: ownership[key]
            for key in ("run_id", "execution_arn", "instance_id", "owner_token")
        }
        prefix = f"runs/{self.run_id}/"
        ssm_request = build_audio_safeguard_request(
            run_id=self.run_id, bucket="offline-test", probe_token=token
        )
        claim = {
            **identity,
            "probe_token": token,
            "requested_at": (now - timedelta(seconds=60)).isoformat(),
            "request_sha256": json_sha256(ssm_request),
            "output_prefix": ssm_request["OutputS3KeyPrefix"],
            "automatic_retry_allowed": False,
            "generation_requested": False,
        }
        command_id = "12345678-1234-1234-1234-123456789abc"
        records = {
            WORKER_LOCK_KEY: ownership,
            prefix + "audio_instance_start_claim.json": {
                **identity,
                "requested_at": (now - timedelta(seconds=180)).isoformat(),
                "automatic_retry_allowed": False,
            },
            prefix + "audio_safeguard_submission_claim.json": claim,
            prefix + "audio_safeguard_submission.json": {
                **claim,
                "status": "audio_safeguard_check_submitted",
                "command_id": command_id,
                "claim_key": prefix + "audio_safeguard_submission_claim.json",
            },
        }

        saved_bodies = {}

        def read(**kwargs):
            self.assertEqual(kwargs["Bucket"], "offline-test")
            return {
                "ETag": request["lock_etag"],
                "Body": io.BytesIO(saved_bodies.get(
                    kwargs["Key"], json.dumps(records[kwargs["Key"]]).encode()
                )),
            }

        def preserve(**kwargs):
            self.assertEqual(kwargs["IfNoneMatch"], "*")
            key = kwargs["Key"]
            if key in records:
                raise ClientError(
                    {"Error": {"Code": "PreconditionFailed"},
                     "ResponseMetadata": {"HTTPStatusCode": 412}},
                    "PutObject",
                )
            records[key] = json.loads(kwargs["Body"])
            saved_bodies[key] = kwargs["Body"]
            return {}

        client.get_object.side_effect = read
        client.put_object.side_effect = preserve
        request["ssm_client"] = Mock()
        request["ssm_client"].get_command_invocation.return_value = {
            "CommandId": command_id,
            "InstanceId": AUDIO_INSTANCE_ID,
            "DocumentName": "AWS-RunShellScript",
            "Comment": f"MIP safeguards {token}",
            "PluginName": "aws:runShellScript",
            "Status": "Success",
            "StatusDetails": "Success",
            "ResponseCode": 0,
            "ExecutionStartDateTime": (now - timedelta(seconds=20)).isoformat(),
            "ExecutionEndDateTime": (now - timedelta(seconds=10)).isoformat(),
            "StandardErrorContent": "",
            "StandardOutputContent": json.dumps({
                "status": "audio_safeguards_verified",
                "probe_token": token,
                "boot_id": "23456789-1234-1234-1234-123456789abc",
                "uptime_seconds": 150.0,
                "shutdown_deadline_monotonic_us": 3_600_000_000,
                "generation_requested": False,
            }),
        }
        return request, records

    def test_safeguard_retrieval_verifies_and_preserves_repeatable_result(self):
        request, records = self.make_safeguard_retrieval_request()
        result = retrieve_audio_safeguard_check(**request)
        self.assertEqual(result["status"], "audio_safeguards_verified")
        self.assertFalse(result["generation_requested"])
        self.assertEqual(
            records[result["result_key"]]["boot_id"], result["boot_id"]
        )
        self.assertEqual(retrieve_audio_safeguard_check(**request), result)
        request["ssm_client"].get_command_invocation.assert_called_with(
            CommandId="12345678-1234-1234-1234-123456789abc",
            InstanceId=AUDIO_INSTANCE_ID,
        )
        request["ssm_client"].send_command.assert_not_called()
        request["client"].delete_object.assert_not_called()

    def test_safeguard_retrieval_checks_owner_before_ssm(self):
        request, _ = self.make_safeguard_retrieval_request()
        request["owner_token"] = "wrong-owner"
        with self.assertRaises(ValueError):
            retrieve_audio_safeguard_check(**request)
        request["client"].get_object.assert_called_once()
        request["ssm_client"].get_command_invocation.assert_not_called()
        request["client"].put_object.assert_not_called()

    def test_safeguard_retrieval_rejects_changed_saved_records(self):
        for filename, field, value in (
            ("audio_instance_start_claim.json", "execution_arn", "wrong"),
            ("audio_instance_start_claim.json", "automatic_retry_allowed", 0),
            ("audio_safeguard_submission_claim.json", "owner_token", "wrong"),
            ("audio_safeguard_submission_claim.json", "request_sha256", "0" * 64),
            ("audio_safeguard_submission_claim.json", "output_prefix", "wrong"),
            ("audio_safeguard_submission_claim.json", "generation_requested", 0),
            ("audio_safeguard_submission.json", "command_id", "not-a-uuid"),
            ("audio_safeguard_submission.json", "probe_token", "b" * 32),
            ("audio_safeguard_submission.json", "generation_requested", 0),
        ):
            with self.subTest(filename=filename, field=field):
                request, records = self.make_safeguard_retrieval_request()
                records[f"runs/{self.run_id}/{filename}"][field] = value
                with self.assertRaises(ValueError):
                    retrieve_audio_safeguard_check(**request)
                request["ssm_client"].get_command_invocation.assert_not_called()
                request["client"].put_object.assert_not_called()

    def test_safeguard_retrieval_waits_without_resubmitting(self):
        for status in ("Pending", "InProgress", "Delayed", "Cancelling"):
            with self.subTest(status=status):
                request, _ = self.make_safeguard_retrieval_request()
                ssm = request["ssm_client"]
                ssm.get_command_invocation.return_value["Status"] = status
                result = retrieve_audio_safeguard_check(**request)
                self.assertEqual(result["status"], "audio_safeguard_check_pending")
                ssm.get_command_invocation.assert_called_once()
                ssm.send_command.assert_not_called()
                request["client"].put_object.assert_not_called()

    def test_safeguard_retrieval_handles_visibility_and_propagates_errors(self):
        for code in ("InvocationDoesNotExist", "AccessDeniedException"):
            with self.subTest(code=code):
                request, _ = self.make_safeguard_retrieval_request()
                ssm = request["ssm_client"]
                error = ClientError({"Error": {"Code": code}}, "GetCommandInvocation")
                ssm.get_command_invocation.side_effect = error
                if code == "InvocationDoesNotExist":
                    result = retrieve_audio_safeguard_check(**request)
                    self.assertEqual(result["reason"], "invocation_not_visible")
                else:
                    with self.assertRaises(ClientError) as caught:
                        retrieve_audio_safeguard_check(**request)
                    self.assertIs(caught.exception, error)
                ssm.get_command_invocation.assert_called_once()
                ssm.send_command.assert_not_called()
                request["client"].put_object.assert_not_called()

    def test_safeguard_retrieval_bounds_polling_without_cancelling(self):
        request, records = self.make_safeguard_retrieval_request()
        prefix = f"runs/{self.run_id}/"
        old = datetime.now(timezone.utc) - timedelta(minutes=11)
        records[prefix + "audio_instance_start_claim.json"]["requested_at"] = (
            old - timedelta(minutes=1)
        ).isoformat()
        for name in (
            "audio_safeguard_submission_claim.json",
            "audio_safeguard_submission.json",
        ):
            records[prefix + name]["requested_at"] = old.isoformat()
        with self.assertRaises(TimeoutError):
            retrieve_audio_safeguard_check(**request)
        request["ssm_client"].get_command_invocation.assert_not_called()
        request["ssm_client"].send_command.assert_not_called()
        request["ssm_client"].cancel_command.assert_not_called()

    def test_safeguard_retrieval_preserves_terminal_failure(self):
        for status in ("Failed", "TimedOut", "Cancelled"):
            with self.subTest(status=status):
                request, records = self.make_safeguard_retrieval_request()
                invocation = request["ssm_client"].get_command_invocation.return_value
                invocation.update(Status=status, StatusDetails=status, ResponseCode=1)
                result = retrieve_audio_safeguard_check(**request)
                self.assertEqual(result["status"], "audio_safeguard_check_failed")
                self.assertEqual(
                    records[result["evidence_key"]]["invocation"]["Status"], status
                )
                request["ssm_client"].send_command.assert_not_called()

    def test_safeguard_retrieval_rejects_mismatched_invocation(self):
        for field, value in (
            ("CommandId", "23456789-1234-1234-1234-123456789abc"),
            ("InstanceId", "i-wrong"),
            ("DocumentName", "wrong"),
            ("Comment", "wrong"),
            ("Status", "Unknown"),
        ):
            with self.subTest(field=field):
                request, _ = self.make_safeguard_retrieval_request()
                request["ssm_client"].get_command_invocation.return_value[field] = value
                with self.assertRaises(ValueError):
                    retrieve_audio_safeguard_check(**request)
                request["client"].put_object.assert_not_called()

    def test_safeguard_retrieval_rejects_invalid_success_evidence(self):
        for scenario in (
            "nonzero_exit", "boolean_exit", "stderr", "details",
            "wrong_token", "old_boot", "expired_now", "future", "naive",
            "before_submission",
        ):
            with self.subTest(scenario=scenario):
                request, records = self.make_safeguard_retrieval_request()
                invocation = request["ssm_client"].get_command_invocation.return_value
                probe = json.loads(invocation["StandardOutputContent"])
                if scenario == "nonzero_exit":
                    invocation["ResponseCode"] = 1
                elif scenario == "boolean_exit":
                    invocation["ResponseCode"] = False
                elif scenario == "stderr":
                    invocation["StandardErrorContent"] = "unexpected error"
                elif scenario == "details":
                    invocation["StatusDetails"] = "Failed"
                elif scenario == "wrong_token":
                    probe["probe_token"] = "b" * 32
                elif scenario == "old_boot":
                    probe["uptime_seconds"] = 600
                elif scenario == "expired_now":
                    probe["shutdown_deadline_monotonic_us"] = 155_000_000
                elif scenario == "future":
                    invocation["ExecutionEndDateTime"] = (
                        datetime.now(timezone.utc) + timedelta(minutes=1)
                    ).isoformat()
                elif scenario == "naive":
                    invocation["ExecutionEndDateTime"] = "2026-11-06T10:00:00"
                else:
                    invocation["ExecutionStartDateTime"] = (
                        datetime.now(timezone.utc) - timedelta(minutes=2)
                    ).isoformat()
                invocation["StandardOutputContent"] = json.dumps(probe)
                with self.assertRaises(ValueError):
                    retrieve_audio_safeguard_check(**request)
                prefix = f"runs/{self.run_id}/"
                self.assertIn(prefix + "audio_safeguard_invocation.json", records)
                self.assertNotIn(prefix + "audio_safeguard_result.json", records)
                request["ssm_client"].send_command.assert_not_called()

    def test_safeguard_retrieval_does_not_hide_evidence_write_failure(self):
        request, _ = self.make_safeguard_retrieval_request()
        request["client"].put_object.side_effect = TimeoutError("uncertain write")
        with self.assertRaises(TimeoutError):
            retrieve_audio_safeguard_check(**request)
        request["client"].put_object.assert_called_once()
        request["ssm_client"].get_command_invocation.assert_called_once()
        request["ssm_client"].send_command.assert_not_called()


    def make_safeguard_submission_request(self):
        request = self.make_release_request("running")
        request["ssm_client"] = Mock()

        def send_once(**kwargs):
            # The submission claim must exist before SSM is called.
            request["client"].put_object.assert_called_once()
            saved = request["client"].put_object.call_args.kwargs
            claim = json.loads(saved["Body"])

            self.assertEqual(saved["IfNoneMatch"], "*")
            self.assertEqual(
                saved["Key"],
                f"runs/{self.run_id}/audio_safeguard_submission_claim.json",
            )
            self.assertEqual(claim["owner_token"], request["owner_token"])
            self.assertEqual(claim["request_sha256"], json_sha256(kwargs))
            self.assertFalse(claim["automatic_retry_allowed"])
            self.assertFalse(claim["generation_requested"])

            self.assertEqual(kwargs["InstanceIds"], [AUDIO_INSTANCE_ID])
            self.assertEqual(kwargs["DocumentName"], "AWS-RunShellScript")
            self.assertEqual(
                kwargs["Parameters"]["commands"],
                [
                    build_audio_safeguard_command(
                        probe_token=claim["probe_token"],
                    )
                ],
            )

            return {
                "Command": {
                    "CommandId": "12345678-1234-1234-1234-123456789abc",
                    "InstanceIds": [AUDIO_INSTANCE_ID],
                    "DocumentName": "AWS-RunShellScript",
                    "Comment": kwargs["Comment"],
                }
            }

        request["ssm_client"].send_command.side_effect = send_once
        return request

    def test_safeguard_submission_preserves_claim_and_command_receipt(self):
        request = self.make_safeguard_submission_request()

        with patch(
            "cloud.audio_service.check_audio_worker_connection",
            return_value={"status": "audio_worker_connection_ready"},
        ) as connection:
            result = submit_audio_safeguard_check(**request)

        connection.assert_called_once_with(**request)
        request["ssm_client"].send_command.assert_called_once()
        self.assertEqual(request["client"].put_object.call_count, 2)

        saved = request["client"].put_object.call_args.kwargs
        receipt = json.loads(saved["Body"])
        self.assertEqual(saved["IfNoneMatch"], "*")
        self.assertEqual(
            saved["Key"],
            f"runs/{self.run_id}/audio_safeguard_submission.json",
        )
        self.assertEqual(
            receipt["command_id"],
            "12345678-1234-1234-1234-123456789abc",
        )
        self.assertEqual(
            result["status"],
            "audio_safeguard_check_submitted",
        )
        self.assertEqual(result["command_id"], receipt["command_id"])
        self.assertFalse(result["generation_requested"])

    def test_safeguard_submission_waits_for_connection(self):
        request = self.make_safeguard_submission_request()
        pending = {
            "status": "audio_worker_connection_pending",
            "reason": "ssm_not_fresh_online",
        }

        with patch(
            "cloud.audio_service.check_audio_worker_connection",
            return_value=pending,
        ):
            result = submit_audio_safeguard_check(**request)

        self.assertEqual(result, pending)
        request["client"].put_object.assert_not_called()
        request["ssm_client"].send_command.assert_not_called()

    def test_safeguard_claim_failure_prevents_ssm_submission(self):
        existing_claim = ClientError(
            {
                "Error": {"Code": "PreconditionFailed"},
                "ResponseMetadata": {"HTTPStatusCode": 412},
            },
            "PutObject",
        )

        for error in (
            existing_claim,
            TimeoutError("Uncertain claim write"),
        ):
            with self.subTest(error=str(error)):
                request = self.make_safeguard_submission_request()
                request["client"].put_object.side_effect = error

                with patch(
                    "cloud.audio_service.check_audio_worker_connection",
                    return_value={
                        "status": "audio_worker_connection_ready",
                    },
                ):
                    with self.assertRaises(type(error)) as caught:
                        submit_audio_safeguard_check(**request)

                self.assertIs(caught.exception, error)
                request["client"].put_object.assert_called_once()
                request["ssm_client"].send_command.assert_not_called()
                request["client"].delete_object.assert_not_called()

    def test_uncertain_safeguard_submission_is_not_retried(self):
        for scenario in ("timeout", "malformed_response"):
            with self.subTest(scenario=scenario):
                request = self.make_safeguard_submission_request()
                send = request["ssm_client"].send_command

                if scenario == "timeout":
                    send.side_effect = TimeoutError(
                        "Uncertain SSM submission"
                    )
                    expected_error = TimeoutError
                else:
                    send.side_effect = None
                    send.return_value = {"Command": {}}
                    expected_error = RuntimeError

                with patch(
                    "cloud.audio_service.check_audio_worker_connection",
                    return_value={
                        "status": "audio_worker_connection_ready",
                    },
                ):
                    with self.assertRaises(expected_error):
                        submit_audio_safeguard_check(**request)

                send.assert_called_once()
                request["client"].put_object.assert_called_once()
                request["client"].delete_object.assert_not_called()

    def test_safeguard_receipt_failure_does_not_resubmit(self):
        request = self.make_safeguard_submission_request()
        error = TimeoutError("Uncertain receipt write")
        request["client"].put_object.side_effect = [{}, error]

        with patch(
            "cloud.audio_service.check_audio_worker_connection",
            return_value={"status": "audio_worker_connection_ready"},
        ):
            with self.assertRaises(TimeoutError) as caught:
                submit_audio_safeguard_check(**request)

        self.assertIs(caught.exception, error)
        request["ssm_client"].send_command.assert_called_once()
        self.assertEqual(request["client"].put_object.call_count, 2)
        request["client"].delete_object.assert_not_called()

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