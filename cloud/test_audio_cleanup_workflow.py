"""Offline routing checks; AWS validation and live execution remain separate."""

import copy
import unittest

from cloud.step_functions.audio_cleanup import build_unacknowledged_cleanup_states
from cloud.test_text_workflow import matches_rule


class AudioCleanupWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.instance_id = "i-0acf87a780fb97103"
        self.states = build_unacknowledged_cleanup_states(
            lambda_arn="arn:aws:lambda:us-east-2:340015815972:function:mip_generate_text",
            instance_id=self.instance_id,
        )
        self.data = {
            "run_context": {"run_id": "2026-11-13-999"},
            "audio_worker": {"execution_arn": "execution-under-test"},
            "audio_failure": {"Error": "OriginalFailure"},
            "audio_cleanup": {
                "started_at": "2026-11-13T04:00:00Z",
                "stop_attempted": False,
                "poll_count": 0,
            },
        }

    def route(self, name):
        state = self.states[name]
        for rule in state["Choices"]:
            if matches_rule(rule, self.data):
                return rule["Next"]
        return state["Default"]

    def result(self, status, state="running"):
        return {
            "status": status,
            "run_id": self.data["run_context"]["run_id"],
            "execution_arn": self.data["audio_worker"]["execution_arn"],
            "instance_id": self.instance_id,
            "instance_state": state,
            "generation_requested": False,
        }

    def test_shutdown_routes_pending_confirmed_and_deadline(self):
        for status, target in (
            ("audio_worker_shutdown_pending", "ChooseUnacknowledgedAudioStop"),
            ("audio_worker_shutdown_confirmed", "AudioFailedWorkerStoppedOwnershipRetained"),
            ("audio_worker_shutdown_deadline_exceeded", "AudioShutdownDeadlineExceeded"),
            ("unexpected", "AudioCleanupUnresolved"),
        ):
            with self.subTest(status=status):
                self.data["audio_cleanup_observation"] = self.result(status)
                self.assertEqual(self.route("CheckUnacknowledgedAudioShutdown"), target)

    def test_result_identity_and_flags_must_match(self):
        for choice, path, status in (
            ("CheckUnacknowledgedAudioShutdown", "audio_cleanup_observation", "audio_worker_shutdown_confirmed"),
            ("CheckUnacknowledgedAudioStop", "audio_cleanup_stop", "audio_worker_stop_requested"),
        ):
            for field, value in (
                ("run_id", "wrong"),
                ("execution_arn", "wrong"),
                ("instance_id", "wrong"),
                ("generation_requested", True),
                ("generation_requested", 0),
            ):
                with self.subTest(choice=choice, field=field, value=value):
                    result = self.result(status)
                    result[field] = value
                    self.data[path] = result
                    self.assertEqual(self.route(choice), "AudioCleanupUnresolved")

    def test_stop_is_only_attempted_once_and_only_when_running(self):
        for state in ("pending", "running", "stopping"):
            for attempted in (False, True):
                with self.subTest(state=state, attempted=attempted):
                    self.data["audio_cleanup_observation"] = self.result(
                        "audio_worker_shutdown_pending", state
                    )
                    self.data["audio_cleanup"]["stop_attempted"] = attempted
                    target = self.route("ChooseUnacknowledgedAudioStop")
                    expected = "CountUnacknowledgedAudioShutdownPoll"
                    if state == "running" and not attempted:
                        expected = "MarkUnacknowledgedAudioStopAttempt"
                    self.assertEqual(target, expected)

        marker = self.states["MarkUnacknowledgedAudioStopAttempt"]
        self.assertIs(marker["Result"], True)
        self.assertEqual(marker["ResultPath"], "$.audio_cleanup.stop_attempted")
        self.assertEqual(marker["Next"], "StopUnacknowledgedAudioWorker")

    def test_stop_results_return_to_observation_without_resubmission(self):
        for status in (
            "audio_worker_stop_requested", "audio_worker_shutdown_pending",
            "audio_worker_shutdown_confirmed",
        ):
            self.data["audio_cleanup_stop"] = self.result(status)
            self.assertEqual(
                self.route("CheckUnacknowledgedAudioStop"),
                "CountUnacknowledgedAudioShutdownPoll",
            )
        self.data["audio_cleanup_stop"] = self.result("unexpected")
        self.assertEqual(self.route("CheckUnacknowledgedAudioStop"), "AudioCleanupUnresolved")

    def test_polling_preserves_deadline_and_stop_attempt(self):
        begin = self.states["BeginUnacknowledgedAudioCleanup"]
        self.assertEqual(begin["Parameters"]["started_at.$"], "$$.State.EnteredTime")
        count = self.states["CountUnacknowledgedAudioShutdownPoll"]
        self.assertEqual(count["Parameters"]["started_at.$"], "$.audio_cleanup.started_at")
        self.assertEqual(count["Parameters"]["stop_attempted.$"], "$.audio_cleanup.stop_attempted")
        monitor = self.states["ObserveUnacknowledgedAudioShutdown"]
        self.assertEqual(
            monitor["Parameters"]["shutdown_wait_started_at.$"],
            "$.audio_cleanup.started_at",
        )
        for count_value, target in (
            (20, "WaitForUnacknowledgedAudioShutdown"),
            (21, "AudioShutdownDeadlineExceeded"),
            (22, "AudioShutdownDeadlineExceeded"),
        ):
            self.data["audio_cleanup"]["poll_count"] = count_value
            self.assertEqual(self.route("CheckUnacknowledgedAudioShutdownPollLimit"), target)

    def test_cleanup_errors_preserve_original_failure_and_never_retry(self):
        for state in self.states.values():
            self.assertNotIn("Retry", state)
            self.assertNotEqual(state.get("ResultPath"), "$.audio_failure")
            if state["Type"] == "Task":
                self.assertEqual(state["Catch"], [{
                    "ErrorEquals": ["States.ALL"],
                    "ResultPath": "$.audio_cleanup_error",
                    "Next": "AudioCleanupUnresolved",
                }])
        unresolved = self.states["AudioStartupOutcomeUnresolved"]
        self.assertEqual(unresolved["Type"], "Fail")
        self.assertNotIn("Next", unresolved)

    def test_fragment_never_generates_or_releases_and_has_valid_targets(self):
        actions = {
            state["Parameters"]["action"]
            for state in self.states.values() if state["Type"] == "Task"
        }
        self.assertEqual(actions, {"monitor_audio_worker_shutdown", "submit_audio_worker_stop"})
        for state in self.states.values():
            targets = [state[key] for key in ("Next", "Default") if key in state]
            targets += [rule["Next"] for rule in state.get("Choices", [])]
            targets += [item["Next"] for item in state.get("Catch", [])]
            for target in targets:
                self.assertIn(target, self.states)
            self.assertNotEqual(state["Type"], "Succeed")

    def test_each_build_returns_independent_states(self):
        original = copy.deepcopy(self.states)
        other = build_unacknowledged_cleanup_states(lambda_arn="other", instance_id="other")
        other["BeginUnacknowledgedAudioCleanup"]["Parameters"]["stop_attempted"] = True
        self.assertEqual(self.states, original)


if __name__ == "__main__":
    unittest.main()