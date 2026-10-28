"""Offline tests for the cloud briefing submission flow."""

import unittest
import hashlib
import json
from unittest.mock import MagicMock, Mock, patch

from cloud.briefing_service import (
    _load_prior_continuity,
    load_prepared_briefing_run,
    prepare_briefing_run,
    retrieve_briefing_run,
    submit_prepared_briefing_run,
    validate_saved_briefing_run,
)
from cloud.storage import S3RunStorage
from cloud.test_storage import FakeS3
from cloud.text_generation import build_briefing_request


class BriefingSubmissionTests(unittest.TestCase):
    def setUp(self):
        self.run_id = "2026-10-25-996"
        self.run_context = {
            "episode_date": "2026-10-25",
            "timezone": "America/Toronto",
            "as_of_timestamp": "2026-10-25T06:00:00-04:00",
            "run_id": self.run_id,
            "prompt_version": "v1",
        }

        self.storage = S3RunStorage(
            bucket="offline-test",
            client=FakeS3(),
        )

        self.instructions = "Research an offline test briefing."
        self.request = build_briefing_request(
            model="gpt-6.1-sol",
            instructions=self.instructions,
            run_context=self.run_context,
            prior_carry_forward=None,
        )

        self.storage.preserve_json(
            self.run_id,
            "inputs.json",
            {
                "run_context": self.run_context,
                "model": "gpt-6.1-sol",
                "prior_carry_forward": None,
                "continuity_mode": "explicit_without_prior_state",
            },
        )
        self.storage.preserve_json(
            self.run_id,
            "request.json",
            self.request,
        )

        self.response = {
            "id": "resp_offline_test",
            "status": "queued",
            "model": "gpt-6.1-sol",
            "output": [],
        }

        self.client = Mock()
        self.client.responses.create.return_value = self.response

        self.client_context = MagicMock()
        self.client_context.__enter__.return_value = self.client
        self.client_context.__exit__.return_value = False
        self.client_factory = Mock(return_value=self.client_context)

        # Supply a tiny prompt instead of reading production prompt files.
        prompt_patch = patch(
            "cloud.briefing_service.load_prompt_bundle",
            return_value={"briefing": self.instructions},
        )
        prompt_patch.start()
        self.addCleanup(prompt_patch.stop)

    def submit(self):
        return submit_prepared_briefing_run(
            run_id=self.run_id,
            storage=self.storage,
            client_factory=self.client_factory,
        )

    def test_submits_exact_saved_request_and_preserves_response(self):
        result = self.submit()

        self.client.responses.create.assert_called_once_with(
            **self.request
        )
        self.assertEqual(result["status"], "initial_response_preserved")
        self.assertTrue(result["generation_requested"])

        saved_id = self.storage.read_json(
            self.run_id,
            "briefing_response_id.json",
        )
        self.assertEqual(saved_id["response_id"], self.response["id"])
        self.assertEqual(
            self.storage.read_json(
                self.run_id,
                "briefing_response_initial.json",
            ),
            self.response,
        )

    def test_second_attempt_does_not_submit_again(self):
        self.submit()
        result = self.submit()

        self.client.responses.create.assert_called_once()
        self.assertEqual(
            result["status"],
            "existing_claim_submission_blocked",
        )
        self.assertFalse(result["generation_requested"])

    def test_changed_request_is_rejected_before_claim(self):
        key = (
            self.storage.bucket,
            f"runs/{self.run_id}/request.json",
        )
        # Simulate a mismatched saved request inside fake storage only.
        self.storage.client.objects[key] = b'{"model": "different"}'

        with self.assertRaisesRegex(
            ValueError,
            "Saved request input has an unexpected format",
        ):
            self.submit()

        self.client_factory.assert_not_called()
        self.client.responses.create.assert_not_called()
        self.assertTrue(
            self.storage.claim_briefing_submission(self.run_id)
        )

    def test_read_failure_stops_before_client_or_claim(self):
        with patch.object(
            self.storage,
            "read_json",
            side_effect=RuntimeError("Simulated read failure"),
        ):
            with self.assertRaises(RuntimeError):
                self.submit()

        self.client_factory.assert_not_called()
        self.assertTrue(
            self.storage.claim_briefing_submission(self.run_id)
        )

    def test_client_initialization_failure_leaves_run_unclaimed(self):
        self.client_factory.side_effect = RuntimeError(
            "Simulated client initialization failure"
        )

        with self.assertRaises(RuntimeError):
            self.submit()

        self.client.responses.create.assert_not_called()
        self.assertTrue(
            self.storage.claim_briefing_submission(self.run_id)
        )

    def test_claim_failure_prevents_submission(self):
        with patch.object(
            self.storage,
            "claim_briefing_submission",
            side_effect=RuntimeError("Simulated claim failure"),
        ):
            with self.assertRaises(RuntimeError):
                self.submit()

        self.client.responses.create.assert_not_called()

    def test_submission_failure_keeps_claim_and_records_evidence(self):
        self.client.responses.create.side_effect = TimeoutError(
            "Simulated uncertain acceptance"
        )

        with self.assertRaises(TimeoutError):
            self.submit()

        evidence = self.storage.read_json(
            self.run_id,
            "briefing_submission_failure.json",
        )
        self.assertEqual(evidence["stage"], "openai_submission")
        self.assertFalse(evidence["automatic_resubmission_allowed"])

        result = self.submit()

        self.client.responses.create.assert_called_once()
        self.assertFalse(result["generation_requested"])

    def test_response_storage_failure_keeps_claim_and_response_id(self):
        original_preserve = self.storage.preserve_json

        def fail_initial_response(run_id, filename, value):
            if filename == "briefing_response_initial.json":
                raise RuntimeError("Simulated response storage failure")
            return original_preserve(run_id, filename, value)

        with patch.object(
            self.storage,
            "preserve_json",
            side_effect=fail_initial_response,
        ):
            with self.assertRaises(RuntimeError):
                self.submit()

        saved_id = self.storage.read_json(
            self.run_id,
            "briefing_response_id.json",
        )
        self.assertEqual(saved_id["response_id"], self.response["id"])

        evidence = self.storage.read_json(
            self.run_id,
            "briefing_submission_failure.json",
        )
        self.assertEqual(evidence["stage"], "initial_response_storage")
        self.assertEqual(evidence["response_id"], self.response["id"])

        result = self.submit()

        self.client.responses.create.assert_called_once()
        self.assertFalse(result["generation_requested"])

class BriefingRetrievalTests(unittest.TestCase):
    def setUp(self):
        self.run_id = "2026-10-25-995"
        self.response_id = "resp_offline_retrieval"
        self.storage = S3RunStorage(
            bucket="offline-test",
            client=FakeS3(),
        )
        self.storage.preserve_json(
            self.run_id,
            "briefing_response_id.json",
            {
                "run_id": self.run_id,
                "response_id": self.response_id,
            },
        )

        self.response = {
            "id": self.response_id,
            "status": "completed",
            "output": [],
        }
        self.client = Mock()
        self.client.responses.retrieve.return_value = self.response

        client_context = MagicMock()
        client_context.__enter__.return_value = self.client
        client_context.__exit__.return_value = False
        self.client_factory = Mock(return_value=client_context)

    def retrieve(self):
        return retrieve_briefing_run(
            run_id=self.run_id,
            storage=self.storage,
            client_factory=self.client_factory,
        )

    def test_completed_response_is_preserved_without_submission(self):
        result = self.retrieve()

        self.client.responses.retrieve.assert_called_once_with(
            self.response_id,
            include=["web_search_call.action.sources"],
        )
        self.client.responses.create.assert_not_called()
        self.assertEqual(
            result["status"], "completed_response_preserved"
        )
        self.assertFalse(result["generation_requested"])
        self.assertEqual(
            self.storage.read_json(
                self.run_id, result["snapshot_filename"]
            ),
            self.response,
        )

    def test_pending_statuses_are_reported(self):
        for status in ("queued", "in_progress"):
            with self.subTest(status=status):
                self.response["status"] = status
                result = self.retrieve()
                self.assertEqual(result["status"], "generation_pending")
                self.assertEqual(result["response_status"], status)

        self.client.responses.create.assert_not_called()

    def test_unsuccessful_statuses_are_preserved(self):
        for status in ("failed", "cancelled", "incomplete"):
            with self.subTest(status=status):
                self.response["status"] = status
                result = self.retrieve()
                self.assertEqual(
                    result["status"], "generation_unsuccessful"
                )
                self.assertEqual(
                    self.storage.read_json(
                        self.run_id, result["snapshot_filename"]
                    ),
                    self.response,
                )

    def test_repeated_retrieval_preserves_separate_snapshots(self):
        self.response["status"] = "in_progress"
        first = self.retrieve()

        self.response["status"] = "completed"
        second = self.retrieve()

        self.assertNotEqual(
            first["snapshot_filename"], second["snapshot_filename"]
        )
        self.assertEqual(
            self.storage.read_json(
                self.run_id, first["snapshot_filename"]
            )["status"],
            "in_progress",
        )
        self.assertEqual(
            self.storage.read_json(
                self.run_id, second["snapshot_filename"]
            )["status"],
            "completed",
        )
        self.client.responses.create.assert_not_called()

    def test_invalid_reference_stops_before_api_call(self):
        with patch.object(
            self.storage,
            "read_json",
            return_value={
                "run_id": "different-run",
                "response_id": self.response_id,
            },
        ):
            with self.assertRaisesRegex(ValueError, "different run_id"):
                self.retrieve()

        self.client_factory.assert_not_called()

    def test_retrieval_failure_does_not_submit(self):
        self.client.responses.retrieve.side_effect = TimeoutError(
            "Simulated retrieval timeout"
        )

        with self.assertRaises(TimeoutError):
            self.retrieve()

        self.client.responses.create.assert_not_called()

    def test_snapshot_storage_failure_is_not_reported_as_success(self):
        with patch.object(
            self.storage,
            "preserve_json",
            side_effect=RuntimeError("Simulated storage failure"),
        ):
            with self.assertRaises(RuntimeError):
                self.retrieve()

        self.client.responses.create.assert_not_called()

    def test_wrong_response_id_is_preserved_then_rejected(self):
        self.response["id"] = "resp_wrong"

        with self.assertRaisesRegex(ValueError, "does not match"):
            self.retrieve()

        snapshots = [
            key
            for key in self.storage.client.objects
            if "briefing_response_snapshot_" in key[1]
        ]
        self.assertEqual(len(snapshots), 1)
        filename = snapshots[0][1].rsplit("/", 1)[1]
        self.assertEqual(
            self.storage.read_json(self.run_id, filename)["id"],
            "resp_wrong",
        )
        self.client.responses.create.assert_not_called()

class BriefingValidationTests(unittest.TestCase):
    def setUp(self):
        self.run_id = "2026-10-25-994"
        self.response_id = "resp_offline_validation"
        self.filename = "briefing_response_snapshot_offline.json"
        self.storage = S3RunStorage(
            bucket="offline-test",
            client=FakeS3(),
        )
        self.run_context = {
            "episode_date": "2026-10-25",
            "timezone": "America/Toronto",
            "as_of_timestamp": "2026-10-25T06:00:00-04:00",
            "run_id": self.run_id,
            "prompt_version": "v1",
        }
        self.briefing = {
            "briefing_markdown": "# Test briefing\nMarkets were mixed.",
            "carry_forward": {
                "episode_date": "2026-10-25",
                "as_of": "2026-10-25T06:00:00-04:00",
                "topics": [],
            },
        }
        self.snapshot = {
            "id": self.response_id,
            "model": "gpt-6.1-sol",
            "status": "completed",
            "usage": {
                "input_tokens": 100,
                "output_tokens": 50,
                "total_tokens": 150,
            },
            "output": [
                {
                    "type": "web_search_call",
                    "status": "completed",
                    "action": {
                        "sources": [
                            {
                                "url": "https://example.com/markets",
                                "title": "Offline source",
                            }
                        ]
                    },
                },
                {
                    "type": "message",
                    "content": [
                        {
                            "type": "output_text",
                            "text": json.dumps(self.briefing),
                            "annotations": [],
                        }
                    ],
                },
            ],
        }
        self.storage.preserve_json(
            self.run_id,
            "inputs.json",
            {
                "run_context": self.run_context,
                "model": "gpt-6.1-sol",
                "prior_carry_forward": None,
                "continuity_mode": "explicit_without_prior_state",
            },
        )
        self.storage.preserve_json(
            self.run_id,
            "briefing_response_id.json",
            {
                "run_id": self.run_id,
                "response_id": self.response_id,
            },
        )

    def validate(self):
        return validate_saved_briefing_run(
            run_id=self.run_id,
            snapshot_filename=self.filename,
            storage=self.storage,
        )

    def save_snapshot(self):
        self.storage.preserve_json(
            self.run_id, self.filename, self.snapshot
        )

    def assert_no_extracted_artifacts(self):
        for filename in (
            "briefing.md",
            "carry_forward.json",
            "briefing_sources.json",
            "briefing_usage.json",
            "briefing_validation.json",
            "briefing_acceptance.json",
        ):
            self.assertNotIn(
                (
                    self.storage.bucket,
                    f"runs/{self.run_id}/{filename}",
                ),
                self.storage.client.objects,
            )

    def test_valid_response_extracts_artifacts_without_api_client(self):
        self.save_snapshot()

        with patch("cloud.briefing_service.create_client") as factory:
            result = self.validate()

        factory.assert_not_called()
        self.assertEqual(
            result["status"], "briefing_validated_and_preserved"
        )
        self.assertEqual(result["source_count"], 1)
        self.assertEqual(
            self.storage.read_text(self.run_id, "briefing.md"),
            self.briefing["briefing_markdown"],
        )
        self.assertEqual(
            self.storage.read_json(self.run_id, "carry_forward.json"),
            self.briefing["carry_forward"],
        )
        self.assertEqual(
            self.storage.read_json(
                self.run_id, "briefing_usage.json"
            )["usage"],
            self.snapshot["usage"],
        )
        self.assertEqual(
            self.storage.read_json(
                self.run_id, "briefing_validation.json"
            )["status"],
            "validated",
        )
        self.assertTrue(
            all(value == "created" for value in result["artifacts"].values())
        )

    def test_reprocessing_identical_snapshot_is_safe(self):
        self.save_snapshot()
        self.validate()
        result = self.validate()

        self.assertTrue(
            all(value == "unchanged" for value in result["artifacts"].values())
        )

    def test_pending_response_is_rejected_before_extraction(self):
        self.snapshot["status"] = "in_progress"
        self.save_snapshot()

        with self.assertRaisesRegex(ValueError, "not completed"):
            self.validate()

        self.assert_no_extracted_artifacts()

    def test_wrong_response_id_is_rejected_before_extraction(self):
        self.snapshot["id"] = "resp_wrong"
        self.save_snapshot()

        with self.assertRaisesRegex(ValueError, "does not match"):
            self.validate()

        self.assert_no_extracted_artifacts()

    def test_missing_search_evidence_is_rejected(self):
        self.snapshot["output"] = self.snapshot["output"][1:]
        self.save_snapshot()

        with self.assertRaisesRegex(ValueError, "No completed web-search"):
            self.validate()

        self.assert_no_extracted_artifacts()

    def test_invalid_briefing_is_rejected_before_extraction(self):
        self.briefing["carry_forward"]["episode_date"] = "2026-10-24"
        self.snapshot["output"][1]["content"][0]["text"] = json.dumps(
            self.briefing
        )
        self.save_snapshot()

        with self.assertRaisesRegex(ValueError, "episode_date must match"):
            self.validate()

        self.assert_no_extracted_artifacts()

    def test_partial_storage_failure_can_be_reprocessed(self):
        self.save_snapshot()
        original_preserve = self.storage.preserve_json

        def fail_sources(run_id, filename, value):
            if filename == "briefing_sources.json":
                raise RuntimeError("Simulated storage failure")
            return original_preserve(run_id, filename, value)

        with patch.object(
            self.storage, "preserve_json", side_effect=fail_sources
        ):
            with self.assertRaises(RuntimeError):
                self.validate()

        self.assertNotIn(
            (
                self.storage.bucket,
                f"runs/{self.run_id}/briefing_validation.json",
            ),
            self.storage.client.objects,
        )

        result = self.validate()

        self.assertEqual(result["artifacts"]["briefing.md"], "unchanged")
        self.assertEqual(
            result["artifacts"]["briefing_validation.json"], "created"
        )

    def test_acceptance_binds_exact_content_and_identity(self):
        self.save_snapshot()
        self.validate()

        acceptance = self.storage.read_json(
            self.run_id, "briefing_acceptance.json"
        )

        self.assertEqual(acceptance["status"], "accepted")
        self.assertEqual(acceptance["stage"], "briefing")
        self.assertEqual(acceptance["run_id"], self.run_id)
        self.assertEqual(acceptance["response_id"], self.response_id)
        self.assertEqual(acceptance["model"], self.snapshot["model"])
        self.assertEqual(acceptance["schema_version"], 1)
        self.assertEqual(acceptance["policy_version"], "automatic_v1")
        self.assertEqual(
            acceptance["acceptance_method"], "automated_checks"
        )
        self.assertEqual(
            acceptance["eligible_uses"],
            ["script_generation", "continuity"],
        )
        self.assertEqual(acceptance["source_count"], 1)

        expected_hash = hashlib.sha256(
            self.briefing["briefing_markdown"].encode("utf-8")
        ).hexdigest()
        self.assertEqual(
            acceptance["briefing_sha256"], expected_hash
        )

        sources = self.storage.read_json(
            self.run_id, "briefing_sources.json"
        )
        for field, value in (
            ("carry_forward_sha256", self.briefing["carry_forward"]),
            ("run_context_sha256", self.run_context),
            ("sources_sha256", sources),
        ):
            with self.subTest(field=field):
                canonical = json.dumps(
                    value,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                    allow_nan=False,
                )
                expected_hash = hashlib.sha256(
                    canonical.encode("utf-8")
                ).hexdigest()
                self.assertEqual(acceptance[field], expected_hash)

        # Acceptance must not change the existing validation marker.
        self.assertEqual(
            self.storage.read_json(
                self.run_id, "briefing_validation.json"
            ),
            {
                "run_id": self.run_id,
                "response_id": self.response_id,
                "status": "validated",
                "source_count": 1,
            },
        )

    def check_acceptance_storage_recovery(self, failing_filename):
        self.save_snapshot()
        original_preserve = self.storage.preserve_json

        def fail_selected_artifact(run_id, filename, value):
            if filename == failing_filename:
                raise RuntimeError("Simulated storage failure")
            return original_preserve(run_id, filename, value)

        with patch.object(
            self.storage,
            "preserve_json",
            side_effect=fail_selected_artifact,
        ):
            with self.assertRaisesRegex(
                RuntimeError, "Simulated storage failure"
            ):
                self.validate()

        self.assertNotIn(
            (
                self.storage.bucket,
                f"runs/{self.run_id}/briefing_acceptance.json",
            ),
            self.storage.client.objects,
        )

        result = self.validate()
        self.assertEqual(
            result["artifacts"]["briefing_acceptance.json"],
            "created",
        )

        repeated = self.validate()
        self.assertTrue(
            all(
                status == "unchanged"
                for status in repeated["artifacts"].values()
            )
        )

    def test_validation_marker_failure_prevents_acceptance(self):
        self.check_acceptance_storage_recovery(
            "briefing_validation.json"
        )

    def test_prior_continuity_loads_exact_accepted_notes(self):
        self.save_snapshot()
        self.validate()

        new_context = {
            **self.run_context,
            "episode_date": "2026-10-26",
            "as_of_timestamp": "2026-10-26T06:00:00-04:00",
            "run_id": "2026-10-26-001",
        }

        with patch("cloud.briefing_service.create_client") as factory:
            result = _load_prior_continuity(
                prior_run_id=self.run_id,
                run_context=new_context,
                storage=self.storage,
            )

        factory.assert_not_called()
        self.assertEqual(
            result["prior_carry_forward"],
            self.briefing["carry_forward"],
        )

        provenance = result["prior_state_provenance"]
        self.assertEqual(provenance["run_id"], self.run_id)
        self.assertEqual(provenance["episode_date"], "2026-10-25")
        self.assertEqual(provenance["response_id"], self.response_id)

        acceptance = self.storage.read_json(
            self.run_id, "briefing_acceptance.json"
        )
        canonical = json.dumps(
            acceptance,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        self.assertEqual(
            provenance["briefing_acceptance_sha256"],
            hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
        )
        self.assertEqual(
            provenance["carry_forward_sha256"],
            acceptance["carry_forward_sha256"],
        )

    def test_prior_continuity_rejects_same_or_later_episode(self):
        self.save_snapshot()
        self.validate()

        for episode_date in ("2026-10-25", "2026-10-24"):
            with self.subTest(episode_date=episode_date):
                new_context = {
                    **self.run_context,
                    "episode_date": episode_date,
                    "as_of_timestamp": (
                        f"{episode_date}T06:00:00-04:00"
                    ),
                    "run_id": f"{episode_date}-001",
                }

                with self.assertRaisesRegex(
                    ValueError, "strictly before"
                ):
                    _load_prior_continuity(
                        prior_run_id=self.run_id,
                        run_context=new_context,
                        storage=self.storage,
                    )


    def prepare_continuity_fixture(self):
        self.save_snapshot()
        self.validate()

        new_context = {
            **self.run_context,
            "episode_date": "2026-10-26",
            "as_of_timestamp": "2026-10-26T06:00:00-04:00",
            "run_id": "2026-10-26-001",
        }

        prompt_patch = patch(
            "cloud.briefing_service.load_prompt_bundle",
            return_value={"briefing": "Offline briefing instructions."},
        )
        prompt_patch.start()
        self.addCleanup(prompt_patch.stop)

        result = prepare_briefing_run(
            run_context=new_context,
            model="gpt-6.1-sol",
            prior_run_id=self.run_id,
            storage=self.storage,
        )
        return new_context, result

    def test_preparation_freezes_and_reloads_accepted_continuity(self):
        new_context, result = self.prepare_continuity_fixture()
        new_run_id = new_context["run_id"]

        self.assertFalse(result["generation_requested"])

        inputs = self.storage.read_json(new_run_id, "inputs.json")
        self.assertEqual(inputs["continuity_mode"], "accepted_prior_state")
        self.assertEqual(
            inputs["prior_carry_forward"],
            self.briefing["carry_forward"],
        )
        self.assertEqual(
            inputs["prior_state_provenance"]["run_id"],
            self.run_id,
        )

        request = self.storage.read_json(new_run_id, "request.json")
        prefix = "Generate this edition using the supplied inputs:\n"
        payload = json.loads(request["input"][len(prefix):])
        self.assertEqual(
            payload["prior_carry_forward"],
            inputs["prior_carry_forward"],
        )

        loaded = load_prepared_briefing_run(
            run_id=new_run_id,
            storage=self.storage,
        )
        self.assertEqual(loaded["request"], request)

        repeated = prepare_briefing_run(
            run_context=new_context,
            model="gpt-6.1-sol",
            prior_run_id=self.run_id,
            storage=self.storage,
        )
        self.assertTrue(
            all(
                value == "unchanged"
                for value in repeated["artifacts"].values()
            )
        )

    def test_no_prior_preparation_preserves_existing_input_format(self):
        new_context = {
            **self.run_context,
            "run_id": "2026-10-25-993",
        }

        with patch(
            "cloud.briefing_service.load_prompt_bundle",
            return_value={"briefing": "Offline briefing instructions."},
        ):
            prepare_briefing_run(
                run_context=new_context,
                model="gpt-6.1-sol",
                storage=self.storage,
            )
            load_prepared_briefing_run(
                run_id=new_context["run_id"],
                storage=self.storage,
            )

        inputs = self.storage.read_json(
            new_context["run_id"], "inputs.json"
        )
        self.assertEqual(
            inputs,
            {
                "run_context": new_context,
                "model": "gpt-6.1-sol",
                "prior_carry_forward": None,
                "continuity_mode": "explicit_without_prior_state",
            },
        )

    def test_prior_acceptance_failure_blocks_submission_before_claim(self):
        new_context, _ = self.prepare_continuity_fixture()
        original_read = self.storage.read_json

        for failure in ("missing", "unreadable", "mismatched"):
            with self.subTest(failure=failure):
                def read_with_failure(run_id, filename):
                    if (
                        run_id == self.run_id
                        and filename == "briefing_acceptance.json"
                    ):
                        if failure == "missing":
                            raise RuntimeError("Simulated missing acceptance")
                        if failure == "unreadable":
                            raise ValueError("Simulated unreadable acceptance")
                        return {
                            **original_read(run_id, filename),
                            "carry_forward_sha256": "changed",
                        }
                    return original_read(run_id, filename)

                factory = Mock()

                with patch.object(
                    self.storage,
                    "read_json",
                    side_effect=read_with_failure,
                ), patch.object(
                    self.storage,
                    "claim_briefing_submission",
                ) as claim:
                    with self.assertRaises((RuntimeError, ValueError)):
                        submit_prepared_briefing_run(
                            run_id=new_context["run_id"],
                            storage=self.storage,
                            client_factory=factory,
                        )

                factory.assert_not_called()
                claim.assert_not_called()

    def test_changed_frozen_notes_are_rejected(self):
        new_context, _ = self.prepare_continuity_fixture()
        new_run_id = new_context["run_id"]
        inputs = self.storage.read_json(new_run_id, "inputs.json")
        inputs["prior_carry_forward"]["topics"].append({
            "topic": "Changed topic",
            "status": "ongoing",
            "context": "Not part of the accepted prior notes.",
        })

        # Simulate corruption only inside fake offline storage.
        key = (
            self.storage.bucket,
            f"runs/{new_run_id}/inputs.json",
        )
        self.storage.client.objects[key] = json.dumps(inputs).encode("utf-8")

        with self.assertRaisesRegex(
            ValueError, "Saved prior carry-forward does not match"
        ):
            load_prepared_briefing_run(
                run_id=new_run_id,
                storage=self.storage,
            )

    def test_acceptance_storage_failure_can_be_reprocessed(self):
        self.check_acceptance_storage_recovery(
            "briefing_acceptance.json"
        )

class BriefingPreparationRouteTests(unittest.TestCase):
    def setUp(self):
        self.event = {
            "action": "prepare_briefing",
            "model": "gpt-6.1-sol",
            "run_context": {
                "episode_date": "2026-10-28",
                "timezone": "America/Toronto",
                "as_of_timestamp": "2026-10-28T06:00:00-04:00",
                "run_id": "2026-10-28-991",
                "prompt_version": "v1",
            },
        }

    def test_route_passes_selected_prior_run(self):
        from cloud.lambda_function import lambda_handler

        event = {
            **self.event,
            "prior_run_id": "2026-10-27-001",
        }

        with patch(
            "cloud.briefing_service.prepare_briefing_run"
        ) as prepare:
            result = lambda_handler(event, None)

        prepare.assert_called_once_with(
            run_context=self.event["run_context"],
            model=self.event["model"],
            prior_run_id="2026-10-27-001",
        )
        self.assertIs(result, prepare.return_value)

    def test_route_preserves_explicit_no_prior_state(self):
        from cloud.lambda_function import lambda_handler

        event = {
            **self.event,
            "no_prior_state": True,
        }

        with patch(
            "cloud.briefing_service.prepare_briefing_run"
        ) as prepare:
            lambda_handler(event, None)

        prepare.assert_called_once_with(
            run_context=self.event["run_context"],
            model=self.event["model"],
            prior_run_id=None,
        )

    def test_route_rejects_invalid_continuity_choices(self):
        from cloud.lambda_function import lambda_handler

        invalid_choices = [
            {},
            {"no_prior_state": False},
            {"no_prior_state": "true"},
            {"prior_run_id": None},
            {"prior_run_id": ""},
            {"prior_run_id": "   "},
            {"prior_run_id": 123},
            {
                "prior_run_id": "2026-10-27-001",
                "no_prior_state": True,
            },
            {
                "prior_run_id": "2026-10-27-001",
                "no_prior_state": "false",
            },
            {
                "no_prior_state": True,
                "prior_carry_forward": {},
            },
        ]

        for selection in invalid_choices:
            with self.subTest(selection=selection):
                with patch(
                    "cloud.briefing_service.prepare_briefing_run"
                ) as prepare:
                    with self.assertRaises(ValueError):
                        lambda_handler(
                            {**self.event, **selection},
                            None,
                        )

                prepare.assert_not_called()

if __name__ == "__main__":
    unittest.main()