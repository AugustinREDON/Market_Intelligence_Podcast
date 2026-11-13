"""Offline audio graph checks; these do not establish live AWS success."""

import copy
import unittest

from cloud.step_functions.build_workflow import build_definition
from cloud.test_text_workflow import route_choice


class AudioWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.states = build_definition()["States"]
        self.data = {
            "run_context": {"run_id": "2026-11-13-999"},
            "audio_execution": {"arn": "execution-under-test"},
            "audio_worker": {"execution_arn": "execution-under-test"},
            "workflow_budget": {
                "work_deadline": "2026-11-13T00:50:00Z",
                "cleanup_deadline": "2026-11-13T01:00:00Z",
            },
            "_test_context": {
                "State": {"EnteredTime": "2026-11-13T00:01:00Z"},
            },
        }

    def route(self, name):
        return route_choice(self.states, name, self.data)


    def result(self, status, generation=False):
        return {
            "status": status, "run_id": "2026-11-13-999",
            "execution_arn": "execution-under-test",
            "instance_id": "i-0acf87a780fb97103",
            "generation_requested": generation,
        }

    def test_startup_uses_original_execution_time_and_budgeted_action(self):
        parameters = self.states["StartAudioWorker"]["Parameters"]
        self.assertEqual(
            parameters["action"], "start_audio_worker_with_deadline"
        )
        self.assertEqual(
            parameters["execution_started_at.$"], "$$.Execution.StartTime"
        )
        actions = {
            state.get("Parameters", {}).get("action")
            for state in self.states.values()
        }
        self.assertNotIn("start_audio_worker", actions)


    def test_startup_uncertainty_cannot_enter_generation_or_release(self):
        start = self.states["StartAudioWorker"]
        self.assertEqual(start["Catch"][0]["Next"], "AudioStartupOutcomeUnresolved")
        self.data["audio_stage_result"] = self.result("unknown")
        self.assertEqual(
            self.route("CheckAudioWorkerStart"),
            "AudioStartupOutcomeUnresolved",
        )
        self.assertEqual(
            self.states["AudioStartupOutcomeUnresolved"]["Type"], "Fail"
        )
        self.data["audio_stage_result"] = self.result("audio_instance_start_requested")
        self.assertEqual(
            self.route("CheckAudioWorkerStart"),
            "InitializeAudioConnectionPolling",
        )

    def test_pending_ssm_polls_existing_command_instead_of_resubmitting(self):
        for label, status, generation in (
            ("AudioSafeguards", "audio_safeguard_check_pending", False),
            ("AudioHandoff", "audio_handoff_pending", False),
            ("AudioServiceStart", "audio_service_start_pending", True),
        ):
            self.data["audio_stage_result"] = self.result(status, generation)
            self.assertEqual(
                self.route(f"Check{label}Result"), f"Count{label}Poll"
            )
            self.assertEqual(
                self.states[f"WaitFor{label}"]["Next"],
                f"BudgetBeforeRetrieve{label}",
            )
            self.data[f"{label}_poll"] = {"count": 20}
            self.assertEqual(
                self.route(f"Check{label}PollLimit"), "RecordAudioStageFailure"
            )

    def test_acknowledgement_routes_to_completion_and_checks_identity(self):
        result = self.result("audio_service_start_acknowledged", True)
        self.data["audio_stage_result"] = result
        self.assertEqual(
            self.route("CheckAudioServiceStartResult"),
            "InitializeAudioCompletionPolling",
        )
        for field in ("run_id", "execution_arn", "instance_id", "generation_requested"):
            changed = copy.deepcopy(result)
            changed[field] = "wrong"
            self.data["audio_stage_result"] = changed
            self.assertEqual(
                self.route("CheckAudioServiceStartResult"), "RecordAudioStageFailure"
            )

    def test_verified_audio_requires_shutdown_then_guarded_release(self):
        self.data["audio_completion"] = self.result("audio_completion_verified")
        self.assertEqual(
            self.route("CheckAudioCompletionResult"),
            "InitializeAcknowledgedAudioShutdownPolling",
        )
        self.data["audio_shutdown"] = self.result("audio_worker_shutdown_confirmed")
        self.assertEqual(
            self.route("CheckAcknowledgedAudioShutdown"),
            "ReleaseAcknowledgedAudioWorker",
        )
        release = self.states["ReleaseAcknowledgedAudioWorker"]
        self.assertEqual(
            release["Parameters"]["action"], "release_acknowledged_audio_worker"
        )
        self.data["audio_release"] = self.result("audio_worker_not_stopped")
        self.assertEqual(
            self.route("CheckAcknowledgedAudioRelease"), "AudioCleanupUnresolved"
        )
        self.data["audio_release"] = self.result("audio_worker_released")
        self.assertEqual(
            self.route("CheckAcknowledgedAudioRelease"), "ChooseFinalAudioOutcome"
        )
        self.assertEqual(self.route("ChooseFinalAudioOutcome"), "AudioPipelineSucceeded")
        self.data["audio_failure"] = {"Error": "EarlierFailure"}
        self.assertEqual(self.route("ChooseFinalAudioOutcome"), "AudioFailedAfterCleanup")

    def test_completion_failures_preserve_failure_while_cleanup_errors_are_separate(self):
        monitor = self.states["MonitorAudioCompletion"]
        self.assertEqual(monitor["Catch"][0]["ResultPath"], "$.audio_failure")
        self.assertEqual(
            monitor["Catch"][0]["Next"], "InitializeAcknowledgedAudioShutdownPolling"
        )
        for name in ("StopAcknowledgedAudioWorker", "ReleaseAcknowledgedAudioWorker"):
            catcher = self.states[name]["Catch"][0]
            self.assertEqual(catcher["ResultPath"], "$.audio_cleanup_error")
            self.assertEqual(catcher["Next"], "AudioCleanupUnresolved")
        for status in (
            "audio_worker_stopped_without_completion",
            "audio_completion_deadline_exceeded",
        ):
            self.data["audio_completion"] = self.result(status)
            self.assertEqual(
                self.route("CheckAudioCompletionResult"), "RecordAudioCompletionFailure"
            )

    def test_acknowledged_cleanup_preserves_deadline_and_never_repeats_stop(self):
        self.data["audio_shutdown"] = {
            **self.result("audio_worker_shutdown_pending"),
            "instance_state": "running",
        }
        for attempted, target in (
            (False, "MarkAcknowledgedAudioStopAttempt"),
            (True, "CountAcknowledgedAudioShutdownPoll"),
        ):
            self.data["AcknowledgedAudioShutdown_poll"] = {"stop_attempted": attempted}
            self.assertEqual(self.route("ChooseAcknowledgedAudioStop"), target)
        marker = self.states["MarkAcknowledgedAudioStopAttempt"]
        self.assertIs(marker["Result"], True)
        count = self.states["CountAcknowledgedAudioShutdownPoll"]["Parameters"]
        self.assertIn("started_at.$", count)
        self.assertIn("stop_attempted.$", count)

    def test_work_cutoff_is_shared_across_text_startup_and_audio(self):
        for time_value, expired in (
            ("2026-11-13T00:49:59Z", False),
            ("2026-11-13T00:50:00Z", True),
            ("2026-11-13T00:55:00Z", True),
        ):
            self.data["_test_context"]["State"]["EnteredTime"] = time_value
            for operation, failure in (
                ("SubmitBriefing", "WorkflowWorkDeadlineExceeded"),
                ("SubmitScript", "WorkflowWorkDeadlineExceeded"),
                ("StartAudioWorker", "WorkflowWorkDeadlineOwnershipRetained"),
                ("SubmitAudioServiceStart", "RecordWorkDeadlineBeforeAcknowledgement"),
                ("MonitorAudioCompletion", "RecordWorkDeadlineAfterAcknowledgement"),
            ):
                with self.subTest(time=time_value, operation=operation):
                    self.assertEqual(
                        self.route(f"BudgetBefore{operation}"),
                        failure if expired else operation,
                    )

    def test_cleanup_gets_only_time_remaining_before_original_hour(self):
        # Starting cleanup at minute 59 must not grant another ten minutes.
        self.data["AcknowledgedAudioShutdown_poll"] = {
            "started_at": "2026-11-13T00:59:00Z",
            "count": 0,
            "stop_attempted": False,
        }
        for time_value, expired in (
            ("2026-11-13T00:59:59Z", False),
            ("2026-11-13T01:00:00Z", True),
        ):
            self.data["_test_context"]["State"]["EnteredTime"] = time_value
            for operation in (
                "ObserveUnacknowledgedAudioShutdown",
                "StopUnacknowledgedAudioWorker",
                "ObserveAcknowledgedAudioShutdown",
                "StopAcknowledgedAudioWorker",
                "ReleaseAcknowledgedAudioWorker",
            ):
                with self.subTest(time=time_value, operation=operation):
                    self.assertEqual(
                        self.route(f"BudgetBefore{operation}"),
                        "WorkflowCleanupDeadlineExceeded" if expired else operation,
                    )

    def test_deadline_routes_preserve_acknowledgement_distinction(self):
        for name, target in (
            ("RecordWorkDeadlineBeforeAcknowledgement",
             "BeginUnacknowledgedAudioCleanup"),
            ("RecordWorkDeadlineAfterAcknowledgement",
             "InitializeAcknowledgedAudioShutdownPolling"),
        ):
            self.assertEqual(self.states[name]["Next"], target)
            self.assertEqual(self.states[name]["ResultPath"], "$.audio_failure")

    def test_every_task_can_only_be_entered_through_its_budget_guard(self):
        tasks = {
            name for name, state in self.states.items()
            if state["Type"] == "Task"
        }
        for source, state in self.states.items():
            targets = [
                state[key] for key in ("Next", "Default") if key in state
            ]
            targets += [
                rule["Next"] for rule in state.get("Choices", [])
            ]
            targets += [
                item["Next"] for item in state.get("Catch", [])
            ]
            for target in targets:
                if target in tasks:
                    self.assertEqual(source, f"BudgetBefore{target}")

        self.assertEqual(build_definition()["TimeoutSeconds"], 3600)


    def test_every_state_is_reachable_and_no_task_has_retry(self):
        visited = set()
        pending = [build_definition()["StartAt"]]
        while pending:
            name = pending.pop()
            if name in visited:
                continue
            visited.add(name)
            state = self.states[name]
            self.assertNotIn("Retry", state)
            pending.extend(state[key] for key in ("Next", "Default") if key in state)
            pending.extend(rule["Next"] for rule in state.get("Choices", []))
            pending.extend(item["Next"] for item in state.get("Catch", []))
        self.assertEqual(visited, set(self.states))
        actions = {
            state.get("Parameters", {}).get("action")
            for state in self.states.values()
        }
        self.assertNotIn("release_audio_worker", actions)


if __name__ == "__main__":
    unittest.main()