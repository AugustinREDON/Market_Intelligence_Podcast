"""Offline checks for guarded SSM handoff submission and retrieval."""

from datetime import datetime, timezone, timedelta
import io
import json
import unittest
from unittest.mock import Mock, patch

from botocore.exceptions import ClientError
from cloud.acceptance import json_sha256
from cloud import audio_handoff_service as service


class AudioHandoffServiceTests(unittest.TestCase):
    def setUp(self):
        self.run_id = "2026-11-06-997"
        self.prefix = f"runs/{self.run_id}/"
        self.command_id = "12345678-1234-1234-1234-123456789abc"
        self.job = {
            "run_id": self.run_id,
            "bucket": "offline-test",
            "region": "us-east-2",
        }
        self.payload = {
            **self.job,
            "execution_arn": service.audio.EXECUTION_ARN_PREFIX + "handoff-service",
            "instance_id": service.audio.AUDIO_INSTANCE_ID,
            "owner_token": "offline-owner",
            "lock_etag": '"offline-etag"',
            "boot_id": "23456789-1234-1234-1234-123456789abc",
            "handoff_token": "a" * 32,
            "job_sha256": json_sha256(self.job),
            "script_sha256": "b" * 64,
        }
        self.proof = self.enterContext(patch.object(
            service.audio, "retrieve_audio_safeguard_check", return_value={
                "status": "audio_safeguards_verified",
                "boot_id": self.payload["boot_id"],
                "probe_token": self.payload["handoff_token"],
            },
        ))
        self.accepted = self.enterContext(patch.object(
            service, "load_accepted_script", return_value={
                "acceptance": {"script_sha256": self.payload["script_sha256"]}
            },
        ))

    def make_request(self, submitted=False):
        owner = {
            "schema_version": 1, "status": "ownership_held",
            **{key: self.payload[key] for key in (
                "run_id", "execution_arn", "instance_id", "owner_token"
            )},
        }
        objects = {
            service.audio.WORKER_LOCK_KEY: json.dumps(owner).encode(),
            self.prefix + "audio_handoff.json": json.dumps(self.payload).encode(),
            self.prefix + "audio_job.json": json.dumps(self.job).encode(),
        }
        client, ssm = Mock(), Mock()

        def read(**kwargs):
            return {
                "ETag": self.payload["lock_etag"],
                "Body": io.BytesIO(objects[kwargs["Key"]]),
            }

        def put(**kwargs):
            self.assertEqual(kwargs["IfNoneMatch"], "*")
            if kwargs["Key"] in objects:
                raise ClientError(
                    {"Error": {"Code": "PreconditionFailed"},
                     "ResponseMetadata": {"HTTPStatusCode": 412}}, "PutObject"
                )
            objects[kwargs["Key"]] = kwargs["Body"]
            return {}

        def send(**kwargs):
            saved = json.loads(
                objects[self.prefix + "audio_handoff_submission_claim.json"]
            )
            self.assertEqual(saved["request_sha256"], json_sha256(kwargs))
            self.assertFalse(saved["automatic_retry_allowed"])
            now = datetime.now(timezone.utc).isoformat()
            ssm.get_command_invocation.return_value = {
                "CommandId": self.command_id,
                "InstanceId": service.audio.AUDIO_INSTANCE_ID,
                "DocumentName": kwargs["DocumentName"],
                "Comment": kwargs["Comment"],
                "Status": "Success", "StatusDetails": "Success",
                "ResponseCode": 0,
                "ExecutionStartDateTime": now, "ExecutionEndDateTime": now,
                "StandardErrorContent": "",
                "StandardOutputContent": json.dumps({
                    **self.payload, "status": "audio_job_handed_off",
                    "bundle_sha256": service.handoff.BUNDLE_SHA256,
                    "installed_versions": service.handoff.EXPECTED_VERSIONS,
                    "generation_requested": False,
                }),
            }
            return {"Command": {
                "CommandId": self.command_id,
                "InstanceIds": kwargs["InstanceIds"],
                "DocumentName": kwargs["DocumentName"],
                "Comment": kwargs["Comment"],
            }}

        client.get_object.side_effect = read
        client.put_object.side_effect = put
        ssm.send_command.side_effect = send
        request = {
            key: self.payload[key] for key in (
                "run_id", "execution_arn", "owner_token", "lock_etag", "bucket"
            )
        }
        request.update(client=client, ssm_client=ssm)
        if submitted:
            service.submit_audio_handoff(**request)
            client.reset_mock()
            ssm.reset_mock()
        return request, objects

    def make_start_request(self):
        request, objects = self.make_request(submitted=True)
        client, ssm = request["client"], request["ssm_client"]
        client.head_object.side_effect = ClientError(
            {"Error": {"Code": "404"},
             "ResponseMetadata": {"HTTPStatusCode": 404}}, "HeadObject"
        )

        def send(**kwargs):
            claim = json.loads(objects[
                self.prefix + "audio_service_start_submission_claim.json"
            ])
            self.assertEqual(claim["request_sha256"], json_sha256(kwargs))
            self.assertEqual(json.loads(objects[claim["request_key"]]), kwargs)
            self.assertIs(claim["automatic_retry_allowed"], False)
            self.assertIs(claim["generation_requested"], True)
            return {"Command": {
                "CommandId": self.command_id,
                "InstanceIds": kwargs["InstanceIds"],
                "DocumentName": kwargs["DocumentName"],
                "Comment": kwargs["Comment"],
            }}

        ssm.send_command.side_effect = send
        return request, objects

    def make_start_retrieval(self):
        request, objects = self.make_start_request()
        submitted = service.submit_audio_service_start(**request)
        saved = json.loads(objects[submitted["request_key"]])
        now = datetime.now(timezone.utc).isoformat()
        request["ssm_client"].get_command_invocation.return_value = {
            "CommandId": submitted["command_id"],
            "InstanceId": service.audio.AUDIO_INSTANCE_ID,
            "DocumentName": saved["DocumentName"],
            "Comment": saved["Comment"],
            "Status": "Success",
            "StatusDetails": "Success",
            "ResponseCode": 0,
            "ExecutionStartDateTime": now,
            "ExecutionEndDateTime": now,
            "StandardErrorContent": "",
            "StandardOutputContent": json.dumps({
                **self.payload,
                "status": "audio_service_start_acknowledged",
                "startup_claim_key": (
                    self.prefix + "audio_service_start_execution_claim.json"
                ),
                "bundle_sha256": service.handoff.BUNDLE_SHA256,
                "installed_versions": service.handoff.EXPECTED_VERSIONS,
                "generation_requested": True,
            }),
        }
        request["client"].reset_mock()
        request["ssm_client"].reset_mock()
        return request, objects

    def test_start_retrieval_uses_saved_request_and_preserves_acknowledgement(self):
        request, objects = self.make_start_retrieval()
        with patch.object(
            service.handoff,
            "build_audio_handoff_command",
            side_effect=AssertionError("Retrieval must not rebuild commands"),
        ):
            result = service.retrieve_audio_service_start(**request)
            self.assertEqual(service.retrieve_audio_service_start(**request), result)
        self.assertEqual(result["status"], "audio_service_start_acknowledged")
        self.assertIs(result["generation_requested"], True)
        self.assertIn(result["result_key"], objects)
        request["ssm_client"].get_command_invocation.assert_called_with(
            CommandId=self.command_id, InstanceId=service.audio.AUDIO_INSTANCE_ID
        )
        request["ssm_client"].send_command.assert_not_called()

    def test_start_retrieval_handles_pending_failure_and_access_errors(self):
        for status in (
            "Pending", "InProgress", "Delayed", "Cancelling", "missing",
            "Failed", "TimedOut", "Cancelled", "denied",
        ):
            with self.subTest(status=status):
                request, objects = self.make_start_retrieval()
                ssm = request["ssm_client"]
                if status in {"missing", "denied"}:
                    code = (
                        "InvocationDoesNotExist"
                        if status == "missing" else "AccessDeniedException"
                    )
                    ssm.get_command_invocation.side_effect = ClientError(
                        {"Error": {"Code": code}}, "GetCommandInvocation"
                    )
                else:
                    ssm.get_command_invocation.return_value["Status"] = status

                if status == "denied":
                    with self.assertRaises(ClientError):
                        service.retrieve_audio_service_start(**request)
                else:
                    result = service.retrieve_audio_service_start(**request)
                    terminal = status in {"Failed", "TimedOut", "Cancelled"}
                    self.assertEqual(
                        result["status"],
                        "audio_service_start_failed" if terminal
                        else "audio_service_start_pending",
                    )
                    if terminal:
                        self.assertIn(result["evidence_key"], objects)
                ssm.send_command.assert_not_called()
                ssm.cancel_command.assert_not_called()

    def test_start_retrieval_rejects_changed_saved_records_before_ssm(self):
        for filename, field, value in (
            ("audio_service_start_request.json", "Comment", "wrong"),
            ("audio_service_start_submission_claim.json", "request_sha256", "0" * 64),
            ("audio_service_start_submission_claim.json", "generation_requested", 1),
            ("audio_service_start_submission.json", "owner_token", "wrong"),
            ("audio_service_start_submission.json", "command_id", "invalid"),
            ("audio_service_start_submission.json", "automatic_retry_allowed", 0),
        ):
            with self.subTest(filename=filename, field=field):
                request, objects = self.make_start_retrieval()
                record = json.loads(objects[self.prefix + filename])
                record[field] = value
                objects[self.prefix + filename] = json.dumps(record).encode()
                with self.assertRaises(ValueError):
                    service.retrieve_audio_service_start(**request)
                request["ssm_client"].get_command_invocation.assert_not_called()
                request["ssm_client"].send_command.assert_not_called()

    def test_start_retrieval_preserves_evidence_before_rejecting_bad_success(self):
        for scenario in (
            "boot", "flag", "duplicate", "nonzero", "stderr", "timing", "evidence",
        ):
            with self.subTest(scenario=scenario):
                request, objects = self.make_start_retrieval()
                invocation = request["ssm_client"].get_command_invocation.return_value
                output = json.loads(invocation["StandardOutputContent"])
                if scenario == "boot":
                    output["boot_id"] = "34567890-1234-1234-1234-123456789abc"
                elif scenario == "flag":
                    output["generation_requested"] = 1
                elif scenario == "nonzero":
                    invocation["ResponseCode"] = 1
                elif scenario == "stderr":
                    invocation["StandardErrorContent"] = "unexpected"
                elif scenario == "timing":
                    invocation["ExecutionEndDateTime"] = (
                        datetime.now(timezone.utc) + timedelta(minutes=1)
                    ).isoformat()
                elif scenario == "evidence":
                    request["client"].put_object.side_effect = TimeoutError(
                        "uncertain evidence"
                    )
                text = json.dumps(output)
                if scenario == "duplicate":
                    text = text[:-1] + ', "generation_requested": true}'
                invocation["StandardOutputContent"] = text

                error_type = TimeoutError if scenario == "evidence" else ValueError
                with self.assertRaises(error_type):
                    service.retrieve_audio_service_start(**request)
                if scenario != "evidence":
                    self.assertIn(
                        self.prefix + "audio_service_start_invocation.json", objects
                    )
                self.assertNotIn(
                    self.prefix + "audio_service_start_result.json", objects
                )
                request["ssm_client"].send_command.assert_not_called()

    def test_start_retrieval_deadline_does_not_cancel_or_resubmit(self):
        request, objects = self.make_start_retrieval()
        old = (datetime.now(timezone.utc) - timedelta(minutes=11)).isoformat()
        for name in (
            "audio_service_start_submission_claim.json",
            "audio_service_start_submission.json",
        ):
            record = json.loads(objects[self.prefix + name])
            record["requested_at"] = old
            objects[self.prefix + name] = json.dumps(record).encode()
        with self.assertRaises(TimeoutError):
            service.retrieve_audio_service_start(**request)
        request["ssm_client"].get_command_invocation.assert_not_called()
        request["ssm_client"].send_command.assert_not_called()
        request["ssm_client"].cancel_command.assert_not_called()

    def test_service_start_preserves_request_claim_and_receipt(self):
        request, objects = self.make_start_request()
        result = service.submit_audio_service_start(**request)
        self.assertEqual(result["status"], "audio_service_start_submitted")
        saved = json.loads(objects[result["request_key"]])
        self.assertEqual(saved["InstanceIds"], [service.audio.AUDIO_INSTANCE_ID])
        self.assertTrue(saved["Parameters"]["commands"][0].endswith(" start"))
        self.assertEqual(
            json.loads(objects[result["receipt_key"]])["command_id"],
            self.command_id,
        )
        request["ssm_client"].send_command.assert_called_once()

    def test_service_start_requires_handoff_job_and_absent_audio_claim(self):
        for scenario in ("pending", "job", "existing_audio", "denied", "owner"):
            with self.subTest(scenario=scenario):
                request, objects = self.make_start_request()
                if scenario == "pending":
                    request["ssm_client"].get_command_invocation.return_value[
                        "Status"
                    ] = "InProgress"
                elif scenario == "job":
                    objects[self.prefix + "audio_job.json"] = b"{}"
                elif scenario == "existing_audio":
                    request["client"].head_object.side_effect = None
                elif scenario == "denied":
                    request["client"].head_object.side_effect = ClientError(
                        {"Error": {"Code": "AccessDenied"},
                         "ResponseMetadata": {"HTTPStatusCode": 403}},
                        "HeadObject",
                    )
                else:
                    request["owner_token"] = "wrong"

                if scenario == "pending":
                    result = service.submit_audio_service_start(**request)
                    self.assertEqual(result["status"], "audio_handoff_pending")
                else:
                    error_type = ClientError if scenario == "denied" else ValueError
                    with self.assertRaises(error_type):
                        service.submit_audio_service_start(**request)
                request["ssm_client"].send_command.assert_not_called()
                self.assertNotIn(
                    self.prefix + "audio_service_start_submission_claim.json",
                    objects,
                )

    def test_service_start_existing_or_uncertain_submission_never_resends(self):
        for scenario in ("existing", "claim", "send", "malformed", "receipt"):
            with self.subTest(scenario=scenario):
                request, objects = self.make_start_request()
                client, ssm = request["client"], request["ssm_client"]
                claim_key = (
                    self.prefix + "audio_service_start_submission_claim.json"
                )
                if scenario == "existing":
                    objects[claim_key] = b"{}"
                elif scenario in {"claim", "receipt"}:
                    original_put = client.put_object.side_effect
                    fail_key = (
                        claim_key if scenario == "claim"
                        else self.prefix + "audio_service_start_submission.json"
                    )

                    def uncertain_put(**kwargs):
                        result = original_put(**kwargs)
                        if kwargs["Key"] == fail_key:
                            raise TimeoutError("uncertain startup write")
                        return result

                    client.put_object.side_effect = uncertain_put
                elif scenario == "send":
                    ssm.send_command.side_effect = TimeoutError("uncertain send")
                else:
                    ssm.send_command.side_effect = None
                    ssm.send_command.return_value = {"Command": {}}

                expected = (
                    ClientError if scenario == "existing"
                    else RuntimeError if scenario == "malformed"
                    else TimeoutError
                )
                with self.assertRaises(expected):
                    service.submit_audio_service_start(**request)
                self.assertIn(claim_key, objects)
                self.assertEqual(
                    ssm.send_command.call_count,
                    0 if scenario in {"existing", "claim"} else 1,
                )

                ssm.send_command.reset_mock()
                with self.assertRaises(ClientError):
                    service.submit_audio_service_start(**request)
                ssm.send_command.assert_not_called()

    def test_submission_claim_precedes_send_and_receipt_is_saved(self):
        request, objects = self.make_request()
        result = service.submit_audio_handoff(**request)
        self.assertEqual(result["status"], "audio_handoff_submitted")
        self.assertEqual(
            json.loads(objects[result["receipt_key"]])["command_id"],
            self.command_id,
        )
        request["ssm_client"].send_command.assert_called_once()
        request["ssm_client"].get_command_invocation.assert_not_called()
        self.assertFalse(result["generation_requested"])

    def test_wrong_owner_and_changed_preparation_block_submission(self):
        for scenario in ("owner", "descriptor", "job", "script", "boot", "pending"):
            with self.subTest(scenario=scenario):
                request, objects = self.make_request()
                with (
                    patch.dict(self.proof.return_value),
                    patch.dict(self.accepted.return_value["acceptance"]),
                ):
                    if scenario == "owner":
                        request["owner_token"] = "wrong"
                    elif scenario == "descriptor":
                        value = {**self.payload, "lock_etag": '"wrong"'}
                        objects[self.prefix + "audio_handoff.json"] = (
                            json.dumps(value).encode()
                        )
                    elif scenario == "job":
                        objects[self.prefix + "audio_job.json"] = b"{}"
                    elif scenario == "script":
                        self.accepted.return_value["acceptance"]["script_sha256"] = (
                            "c" * 64
                        )
                    elif scenario == "boot":
                        self.proof.return_value["boot_id"] = (
                            "34567890-1234-1234-1234-123456789abc"
                        )
                    else:
                        self.proof.return_value["status"] = (
                            "audio_safeguard_check_pending"
                        )
                    if scenario == "pending":
                        result = service.submit_audio_handoff(**request)
                        self.assertEqual(
                            result["status"], "audio_safeguard_check_pending"
                        )
                    else:
                        with self.assertRaises(ValueError):
                            service.submit_audio_handoff(**request)
                request["ssm_client"].send_command.assert_not_called()
                request["client"].put_object.assert_not_called()

    def test_existing_or_uncertain_claim_never_authorizes_send(self):
        for scenario in ("existing", "uncertain"):
            with self.subTest(scenario=scenario):
                request, objects = self.make_request()
                if scenario == "existing":
                    objects[self.prefix + "audio_handoff_submission_claim.json"] = b"{}"
                    error_type = ClientError
                else:
                    request["client"].put_object.side_effect = TimeoutError(
                        "uncertain claim"
                    )
                    error_type = TimeoutError
                with self.assertRaises(error_type):
                    service.submit_audio_handoff(**request)
                request["ssm_client"].send_command.assert_not_called()

    def test_uncertain_send_or_receipt_failure_is_not_resubmitted(self):
        for scenario in ("timeout", "malformed", "receipt"):
            with self.subTest(scenario=scenario):
                request, objects = self.make_request()
                ssm, client = request["ssm_client"], request["client"]
                if scenario == "timeout":
                    ssm.send_command.side_effect = TimeoutError("uncertain send")
                    error_type = TimeoutError
                elif scenario == "malformed":
                    ssm.send_command.side_effect = None
                    ssm.send_command.return_value = {"Command": {}}
                    error_type = RuntimeError
                else:
                    original_put = client.put_object.side_effect

                    def fail_receipt(**kwargs):
                        if kwargs["Key"].endswith("/audio_handoff_submission.json"):
                            raise TimeoutError("uncertain receipt")
                        return original_put(**kwargs)

                    client.put_object.side_effect = fail_receipt
                    error_type = TimeoutError
                with self.assertRaises(error_type):
                    service.submit_audio_handoff(**request)
                ssm.send_command.assert_called_once()
                self.assertIn(
                    self.prefix + "audio_handoff_submission_claim.json", objects
                )
                ssm.send_command.reset_mock()
                with self.assertRaises(ClientError):
                    service.submit_audio_handoff(**request)
                ssm.send_command.assert_not_called()

    def test_retrieval_verifies_exact_output_and_is_repeatable(self):
        request, objects = self.make_request(submitted=True)
        result = service.retrieve_audio_handoff(**request)
        self.assertEqual(result["status"], "audio_job_handed_off")
        self.assertEqual(result["job_sha256"], self.payload["job_sha256"])
        self.assertEqual(service.retrieve_audio_handoff(**request), result)
        self.assertIn(result["result_key"], objects)
        request["ssm_client"].get_command_invocation.assert_called_with(
            CommandId=self.command_id, InstanceId=service.audio.AUDIO_INSTANCE_ID
        )
        request["ssm_client"].send_command.assert_not_called()
        request["client"].delete_object.assert_not_called()

    def test_retrieval_rejects_changed_records_before_ssm(self):
        for filename, field, value in (
            ("audio_handoff_submission_claim.json", "request_sha256", "0" * 64),
            ("audio_handoff_submission_claim.json", "generation_requested", 0),
            ("audio_handoff_submission.json", "owner_token", "wrong"),
            ("audio_handoff_submission.json", "command_id", "invalid"),
            ("audio_handoff_submission.json", "automatic_retry_allowed", 0),
        ):
            with self.subTest(filename=filename, field=field):
                request, objects = self.make_request(submitted=True)
                record = json.loads(objects[self.prefix + filename])
                record[field] = value
                objects[self.prefix + filename] = json.dumps(record).encode()
                with self.assertRaises(ValueError):
                    service.retrieve_audio_handoff(**request)
                request["ssm_client"].get_command_invocation.assert_not_called()

    def test_retrieval_pending_visibility_and_errors_never_resubmit(self):
        for scenario in (
            "Pending", "InProgress", "Delayed", "Cancelling", "missing", "denied"
        ):
            with self.subTest(scenario=scenario):
                request, _ = self.make_request(submitted=True)
                ssm = request["ssm_client"]
                if scenario in ("missing", "denied"):
                    code = (
                        "InvocationDoesNotExist"
                        if scenario == "missing" else "AccessDeniedException"
                    )
                    ssm.get_command_invocation.side_effect = ClientError(
                        {"Error": {"Code": code}}, "GetCommandInvocation"
                    )
                else:
                    ssm.get_command_invocation.return_value["Status"] = scenario
                if scenario == "denied":
                    with self.assertRaises(ClientError):
                        service.retrieve_audio_handoff(**request)
                else:
                    result = service.retrieve_audio_handoff(**request)
                    self.assertEqual(result["status"], "audio_handoff_pending")
                ssm.get_command_invocation.assert_called_once()
                ssm.send_command.assert_not_called()
                request["client"].put_object.assert_not_called()

    def test_retrieval_timeout_does_not_cancel_or_resubmit(self):
        request, objects = self.make_request(submitted=True)
        old = (datetime.now(timezone.utc) - timedelta(minutes=11)).isoformat()
        for name in (
            "audio_handoff_submission_claim.json",
            "audio_handoff_submission.json",
        ):
            value = json.loads(objects[self.prefix + name])
            value["requested_at"] = old
            objects[self.prefix + name] = json.dumps(value).encode()
        with self.assertRaises(TimeoutError):
            service.retrieve_audio_handoff(**request)
        request["ssm_client"].get_command_invocation.assert_not_called()
        request["ssm_client"].send_command.assert_not_called()
        request["ssm_client"].cancel_command.assert_not_called()

    def test_retrieval_preserves_failure_and_rejects_wrong_invocations(self):
        for status in (
            "Failed", "TimedOut", "Cancelled", "Unknown", "wrong_instance"
        ):
            with self.subTest(status=status):
                request, objects = self.make_request(submitted=True)
                invocation = request["ssm_client"].get_command_invocation.return_value
                if status == "wrong_instance":
                    invocation["InstanceId"] = "i-wrong"
                else:
                    invocation["Status"] = status
                if status in ("Unknown", "wrong_instance"):
                    with self.assertRaises(ValueError):
                        service.retrieve_audio_handoff(**request)
                else:
                    result = service.retrieve_audio_handoff(**request)
                    self.assertEqual(result["status"], "audio_handoff_failed")
                    self.assertIn(result["evidence_key"], objects)
                request["ssm_client"].send_command.assert_not_called()

    def test_success_requires_matching_output_exit_code_and_timing(self):
        for scenario in (
            "boot", "job", "script", "bundle", "versions", "flag", "duplicate",
            "nonzero", "boolean", "stderr", "future", "naive", "before_submission",
        ):
            with self.subTest(scenario=scenario):
                request, objects = self.make_request(submitted=True)
                invocation = request["ssm_client"].get_command_invocation.return_value
                output = json.loads(invocation["StandardOutputContent"])
                changes = {
                    "boot": ("boot_id", "34567890-1234-1234-1234-123456789abc"),
                    "job": ("job_sha256", "0" * 64),
                    "script": ("script_sha256", "0" * 64),
                    "bundle": ("bundle_sha256", "0" * 64),
                    "versions": ("installed_versions", {}),
                    "flag": ("generation_requested", 0),
                }
                if scenario in changes:
                    field, value = changes[scenario]
                    output[field] = value
                elif scenario == "nonzero":
                    invocation["ResponseCode"] = 1
                elif scenario == "boolean":
                    invocation["ResponseCode"] = False
                elif scenario == "stderr":
                    invocation["StandardErrorContent"] = "unexpected"
                elif scenario == "future":
                    invocation["ExecutionEndDateTime"] = (
                        datetime.now(timezone.utc) + timedelta(minutes=1)
                    ).isoformat()
                elif scenario == "naive":
                    invocation["ExecutionEndDateTime"] = "2026-11-06T10:00:00"
                elif scenario == "before_submission":
                    invocation["ExecutionStartDateTime"] = (
                        datetime.now(timezone.utc) - timedelta(minutes=1)
                    ).isoformat()
                text = json.dumps(output)
                if scenario == "duplicate":
                    text = text[:-1] + ', "generation_requested": false}'
                invocation["StandardOutputContent"] = text
                with self.assertRaises(ValueError):
                    service.retrieve_audio_handoff(**request)
                self.assertIn(
                    self.prefix + "audio_handoff_invocation.json", objects
                )
                self.assertNotIn(
                    self.prefix + "audio_handoff_result.json", objects
                )

    def test_evidence_write_failure_does_not_hide_uncertain_handoff(self):
        request, _ = self.make_request(submitted=True)
        request["client"].put_object.side_effect = TimeoutError("uncertain evidence")
        with self.assertRaises(TimeoutError):
            service.retrieve_audio_handoff(**request)
        request["client"].put_object.assert_called_once()
        request["ssm_client"].get_command_invocation.assert_called_once()
        request["ssm_client"].send_command.assert_not_called()

    def test_lambda_routes_handoff_submission_and_retrieval(self):
        from cloud.lambda_function import lambda_handler

        arguments = {
            key: self.payload[key] for key in (
                "run_id", "execution_arn", "owner_token", "lock_etag"
            )
        }
        for action in (
            "submit_audio_handoff",
            "retrieve_audio_handoff",
            "submit_audio_service_start",
            "retrieve_audio_service_start",
        ):
            with self.subTest(action=action):
                expected = {"status": "offline"}
                with patch.object(service, action, return_value=expected) as method:
                    result = lambda_handler({"action": action, **arguments}, None)
                method.assert_called_once_with(**arguments)
                self.assertEqual(result, expected)


if __name__ == "__main__":
    unittest.main()