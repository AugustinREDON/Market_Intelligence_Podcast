"""Offline checks of the text workflow definition and routing rules.

This evaluates only the Choice operators used by this workflow.
AWS definition validation and runtime verification remain separate.
"""

import copy
import json
import unittest
from pathlib import Path

from cloud.step_functions.build_workflow import (
    MAX_POLLS,
    WAIT_SECONDS,
    build_definition,
)


MISSING = object()


def read_path(data, path):
    if not path.startswith("$."):
        raise AssertionError(f"Unsupported path: {path}")

    value = data
    for key in path[2:].split("."):
        if not isinstance(value, dict) or key not in value:
            return MISSING
        value = value[key]
    return value


def matches_rule(rule, data):
    if "And" in rule:
        return all(matches_rule(item, data) for item in rule["And"])

    if "Not" in rule:
        return not matches_rule(rule["Not"], data)

    value = read_path(data, rule["Variable"])
    operators = set(rule) - {"Variable", "Next"}
    if len(operators) != 1:
        raise AssertionError(f"Unsupported rule: {rule}")

    operator = operators.pop()
    expected = rule[operator]

    if operator == "IsPresent":
        return (value is not MISSING) is expected

    if operator == "IsString":
        return isinstance(value, str) is expected

    if value is MISSING:
        return False

    if operator == "StringEquals":
        return isinstance(value, str) and value == expected

    if operator == "StringEqualsPath":
        other = read_path(data, expected)
        return (
            isinstance(value, str)
            and isinstance(other, str)
            and value == other
        )

    if operator == "BooleanEquals":
        return type(value) is bool and value is expected

    if operator == "NumericGreaterThanEquals":
        return (
            type(value) in (int, float)
            and value >= expected
        )

    raise AssertionError(f"Unsupported operator: {operator}")


class TextWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.definition = build_definition()
        self.states = self.definition["States"]
        self.data = {
            "confirm_paid_generation": True,
            "no_prior_state": True,
            "run_context": {"run_id": "2026-10-28-998"},
        }
        self.run_id = self.data["run_context"]["run_id"]

    def route(self, name):
        state = self.states[name]
        self.assertEqual(state["Type"], "Choice")
        for rule in state["Choices"]:
            if matches_rule(rule, self.data):
                return rule["Next"]
        return state["Default"]

    def test_saved_json_matches_builder(self):
        path = (
            Path(__file__).parent
            / "step_functions"
            / "text-workflow.asl.json"
        )
        self.assertEqual(
            json.loads(path.read_text(encoding="utf-8")),
            self.definition,
        )

    def test_all_transition_targets_exist(self):
        targets = []
        for state in self.states.values():
            if "Next" in state:
                targets.append(state["Next"])
            if "Default" in state:
                targets.append(state["Default"])
            for rule in state.get("Choices", []):
                targets.append(rule["Next"])
            for catcher in state.get("Catch", []):
                targets.append(catcher["Next"])

        for target in targets:
            with self.subTest(target=target):
                self.assertIn(target, self.states)

    def test_authorization_requires_boolean_true(self):
        self.assertEqual(
            self.route("CheckAuthorization"),
            "ChooseContinuity",
        )
        for value in (False, None, "true", 1):
            with self.subTest(value=value):
                self.data["confirm_paid_generation"] = value
                self.assertEqual(
                    self.route("CheckAuthorization"),
                    "InvalidInput",
                )

        del self.data["confirm_paid_generation"]
        self.assertEqual(
            self.route("CheckAuthorization"), "InvalidInput"
        )

    def test_continuity_requires_one_explicit_selection(self):
        self.assertEqual(
            self.route("ChooseContinuity"),
            "PrepareBriefingWithoutPrior",
        )

        del self.data["no_prior_state"]
        self.assertEqual(
            self.route("ChooseContinuity"), "InvalidInput"
        )

        self.data["prior_run_id"] = "2026-10-27-001"
        self.assertEqual(
            self.route("ChooseContinuity"),
            "PrepareBriefingWithPrior",
        )

        self.data["no_prior_state"] = True
        self.assertEqual(
            self.route("ChooseContinuity"), "InvalidInput"
        )

        del self.data["no_prior_state"]
        for value in ("", None, 123):
            with self.subTest(value=value):
                self.data["prior_run_id"] = value
                self.assertEqual(
                    self.route("ChooseContinuity"), "InvalidInput"
                )

    def test_preparation_requires_expected_status_and_run(self):
        for stage, status in (
            ("briefing", "inputs_and_request_preserved"),
            ("script", "script_inputs_and_request_preserved"),
        ):
            title = stage.title()
            result = {"status": status, "run_id": self.run_id}
            self.data[f"{stage}_prepare"] = result

            self.assertEqual(
                self.route(f"Check{title}Preparation"),
                f"Submit{title}",
            )

            for field, value in (
                ("status", "unexpected"),
                ("run_id", "different-run"),
            ):
                original = result[field]
                result[field] = value
                self.assertEqual(
                    self.route(f"Check{title}Preparation"),
                    "UnexpectedStageResult",
                )
                result[field] = original

    def test_submissions_are_not_retried_and_require_confirmation(self):
        for stage in ("briefing", "script"):
            title = stage.title()
            task = self.states[f"Submit{title}"]
            self.assertNotIn("Retry", task)
            self.assertEqual(
                task["Parameters"]["confirm_paid_generation.$"],
                "$.confirm_paid_generation",
            )
            self.assertEqual(
                task["Catch"][0]["Next"],
                "SubmissionOutcomeUncertain",
            )

            result = {
                "status": "initial_response_preserved",
                "run_id": self.run_id,
                "generation_requested": True,
                "response_id": f"resp_{stage}",
            }
            self.data[f"{stage}_submit"] = result
            self.assertEqual(
                self.route(f"Check{title}Submission"),
                f"Initialize{title}Polling",
            )

            result["status"] = "existing_claim_submission_blocked"
            self.assertEqual(
                self.route(f"Check{title}Submission"),
                "ExistingSubmissionClaim",
            )

            result["status"] = "unknown"
            self.assertEqual(
                self.route(f"Check{title}Submission"),
                "SubmissionOutcomeUncertain",
            )

    def test_retrieval_routes_pending_completed_and_failure(self):
        for stage in ("briefing", "script"):
            title = stage.title()
            for status, target in (
                ("generation_pending", f"Count{title}Poll"),
                ("completed_response_preserved", f"Validate{title}"),
                ("generation_unsuccessful", "GenerationUnsuccessful"),
                ("unknown", "UnexpectedStageResult"),
            ):
                with self.subTest(stage=stage, status=status):
                    self.data[f"{stage}_retrieve"] = {"status": status}
                    self.assertEqual(
                        self.route(f"Check{title}Retrieval"),
                        target,
                    )

    def test_retrieval_identity_must_match_submission(self):
        for stage in ("briefing", "script"):
            title = stage.title()
            self.data[f"{stage}_submit"] = {"response_id": "resp_saved"}
            result = {
                "run_id": self.run_id,
                "response_id": "resp_saved",
            }
            self.data[f"{stage}_retrieve"] = result

            self.assertEqual(
                self.route(f"Check{title}Identity"),
                f"Check{title}Retrieval",
            )
            result["response_id"] = "resp_other"
            self.assertEqual(
                self.route(f"Check{title}Identity"),
                "UnexpectedStageResult",
            )

    def test_polling_is_bounded_for_each_stage(self):
        self.assertEqual(WAIT_SECONDS, 30)
        self.assertEqual(MAX_POLLS, 60)

        for stage in ("briefing", "script"):
            title = stage.title()
            self.assertEqual(
                self.states[f"Initialize{title}Polling"]["Result"],
                {"count": 0},
            )
            self.assertEqual(
                self.states[f"Count{title}Poll"]["Parameters"],
                {
                    "count.$": (
                        f"States.MathAdd($.{stage}_poll.count, 1)"
                    )
                },
            )

            for count, target in (
                (MAX_POLLS - 1, f"WaitFor{title}"),
                (MAX_POLLS, "PollingLimitReached"),
                (MAX_POLLS + 1, "PollingLimitReached"),
            ):
                self.data[f"{stage}_poll"] = {"count": count}
                self.assertEqual(
                    self.route(f"Check{title}PollingLimit"),
                    target,
                )

    def test_acceptance_requires_exact_completed_snapshot(self):
        for stage, next_state in (
            ("briefing", "PrepareScript"),
            ("script", "TextPipelineAccepted"),
        ):
            title = stage.title()
            snapshot = f"{stage}_response_snapshot_completed.json"
            self.data[f"{stage}_retrieve"] = {
                "response_id": f"resp_{stage}",
                "snapshot_filename": snapshot,
            }
            accepted = {
                "status": f"{stage}_validated_and_preserved",
                "acceptance_status": "accepted",
                "run_id": self.run_id,
                "response_id": f"resp_{stage}",
                "snapshot_filename": snapshot,
            }
            self.data[f"{stage}_validate"] = copy.deepcopy(accepted)

            self.assertEqual(
                self.route(f"Check{title}Acceptance"),
                next_state,
            )
            self.assertEqual(
                self.states[f"Validate{title}"]["Parameters"][
                    "snapshot_filename.$"
                ],
                f"$.{stage}_retrieve.snapshot_filename",
            )

            for field in accepted:
                with self.subTest(stage=stage, field=field):
                    changed = copy.deepcopy(accepted)
                    changed[field] = "wrong"
                    self.data[f"{stage}_validate"] = changed
                    self.assertEqual(
                        self.route(f"Check{title}Acceptance"),
                        "UnexpectedStageResult",
                    )

            missing = copy.deepcopy(accepted)
            del missing["acceptance_status"]
            self.data[f"{stage}_validate"] = missing
            self.assertEqual(
                self.route(f"Check{title}Acceptance"),
                "UnexpectedStageResult",
            )


if __name__ == "__main__":
    unittest.main()