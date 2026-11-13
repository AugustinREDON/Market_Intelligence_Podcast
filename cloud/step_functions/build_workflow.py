"""Build the text workflow definition without calling AWS or OpenAI."""

import json
from pathlib import Path
from cloud.step_functions.audio_workflow import build_audio_states

LAMBDA_ARN = (
    "arn:aws:lambda:us-east-2:340015815972:"
    "function:mip_generate_text"
)
WAIT_SECONDS = 30
MAX_POLLS = 60


def task(payload, result_path, next_state, failure_state):
    """Invoke Lambda synchronously, with no automatic retry."""
    return {
        "Type": "Task",
        "Resource": LAMBDA_ARN,
        "Parameters": payload,
        "ResultPath": result_path,
        "TimeoutSeconds": 125,
        "Catch": [
            {
                "ErrorEquals": ["States.ALL"],
                "ResultPath": "$.workflow_error",
                "Next": failure_state,
            }
        ],
        "Next": next_state,
    }


def choice(rules, default):
    return {
        "Type": "Choice",
        "Choices": rules,
        "Default": default,
    }


def matches(path, value):
    return {"Variable": path, "StringEquals": value}


def same_run(path):
    return {
        "Variable": f"{path}.run_id",
        "StringEqualsPath": "$.run_context.run_id",
    }


def apply_shared_deadlines(states):
    """Gate all tasks against deadlines derived once from execution start."""
    cleanup_actions = {
        "monitor_audio_worker_shutdown",
        "submit_audio_worker_stop",
        "release_acknowledged_audio_worker",
    }
    before_acknowledgement = {
        "check_audio_worker_connection",
        "submit_audio_safeguard_check",
        "retrieve_audio_safeguard_check",
        "prepare_audio_handoff",
        "submit_audio_handoff",
        "retrieve_audio_handoff",
        "submit_audio_service_start",
        "retrieve_audio_service_start",
    }
    gates = {}
    additions = {}
    for name, state in states.items():
        if state["Type"] != "Task":
            continue
        action = state["Parameters"]["action"]
        deadline = "work_deadline"
        expired = "WorkflowWorkDeadlineExceeded"

        if action in cleanup_actions:
            deadline = "cleanup_deadline"
            expired = "WorkflowCleanupDeadlineExceeded"
        elif action == "start_audio_worker":
            expired = "WorkflowWorkDeadlineOwnershipRetained"
        elif action in before_acknowledgement:
            expired = "RecordWorkDeadlineBeforeAcknowledgement"
        elif action == "monitor_audio_completion":
            expired = "RecordWorkDeadlineAfterAcknowledgement"

        gate = f"BudgetBefore{name}"
        gates[name] = gate
        additions[gate] = {
            "Type": "Choice",
            "Choices": [{
                "Variable": "$$.State.EnteredTime",
                "TimestampLessThanPath": f"$.workflow_budget.{deadline}",
                "Next": name,
            }],
            "Default": expired,
        }

    # Redirect existing transitions; each new guard enters its original task.
    for state in states.values():
        for field in ("Next", "Default"):
            if field in state:
                state[field] = gates.get(state[field], state[field])
        for rule in state.get("Choices", []):
            rule["Next"] = gates.get(rule["Next"], rule["Next"])
        for catcher in state.get("Catch", []):
            catcher["Next"] = gates.get(catcher["Next"], catcher["Next"])
    states.update(additions)

    states["InitializeWorkflowBudget"] = {
        "Type": "Pass",
        "QueryLanguage": "JSONata",
        "Output": (
            "{% $merge([$states.input, {'workflow_budget': {"
            "'work_deadline': $fromMillis($toMillis($states.context.Execution.StartTime) + 3000000),"
            "'cleanup_deadline': $fromMillis($toMillis($states.context.Execution.StartTime) + 3600000)"
            "}}]) %}"
        ),
        "Next": "CheckAuthorization",
    }

    for name, next_state in (
        ("RecordWorkDeadlineBeforeAcknowledgement",
         "BeginUnacknowledgedAudioCleanup"),
        ("RecordWorkDeadlineAfterAcknowledgement",
         "InitializeAcknowledgedAudioShutdownPolling"),
    ):
        states[name] = {
            "Type": "Pass",
            "Result": {"Error": "WorkflowWorkDeadlineExceeded"},
            "ResultPath": "$.audio_failure",
            "Next": next_state,
        }

    for name, cause in {
        "WorkflowWorkDeadlineExceeded": (
            "Shared work deadline reached before EC2 startup. "
            "No further work is submitted."
        ),
        "WorkflowWorkDeadlineOwnershipRetained": (
            "Shared work deadline reached before EC2 startup. "
            "Ownership retained for review."
        ),
        "WorkflowCleanupDeadlineExceeded": (
            "Shared one-hour deadline reached. "
            "Ownership retained; inspect shutdown evidence."
        ),
    }.items():
        states[name] = {"Type": "Fail", "Error": name, "Cause": cause}



def build_definition():
    states = {
        "CheckAuthorization": choice(
            [
                {
                    "Variable": "$.confirm_paid_generation",
                    "BooleanEquals": True,
                    "Next": "ChooseContinuity",
                }
            ],
            "InvalidInput",
        ),
        "ChooseContinuity": choice(
            [
                {
                    "And": [
                        {
                            "Variable": "$.prior_run_id",
                            "IsPresent": True,
                        },
                        {
                            "Variable": "$.prior_run_id",
                            "IsString": True,
                        },
                        {
                            "Not": matches("$.prior_run_id", ""),
                        },
                        {
                            "Variable": "$.no_prior_state",
                            "IsPresent": False,
                        },
                    ],
                    "Next": "PrepareBriefingWithPrior",
                },
                {
                    "And": [
                        {
                            "Variable": "$.prior_run_id",
                            "IsPresent": False,
                        },
                        {
                            "Variable": "$.no_prior_state",
                            "BooleanEquals": True,
                        },
                    ],
                    "Next": "PrepareBriefingWithoutPrior",
                },
            ],
            "InvalidInput",
        ),
    }

    briefing_payload = {
        "action": "prepare_briefing",
        "run_context.$": "$.run_context",
        "model.$": "$.model",
    }

    for name, continuity in (
        (
            "PrepareBriefingWithPrior",
            {"prior_run_id.$": "$.prior_run_id"},
        ),
        (
            "PrepareBriefingWithoutPrior",
            {"no_prior_state": True},
        ),
    ):
        states[name] = task(
            {**briefing_payload, **continuity},
            "$.briefing_prepare",
            "CheckBriefingPreparation",
            "StageInvocationFailed",
        )

    for stage, preparation_status, validation_status, next_stage in (
        (
            "briefing",
            "inputs_and_request_preserved",
            "briefing_validated_and_preserved",
            "PrepareScript",
        ),
        (
            "script",
            "script_inputs_and_request_preserved",
            "script_validated_and_preserved",
            "PrepareAudio",
        ),
    ):
        title = stage.title()
        prepared = f"$.{stage}_prepare"
        submitted = f"$.{stage}_submit"
        retrieved = f"$.{stage}_retrieve"
        validated = f"$.{stage}_validate"
        poll = f"$.{stage}_poll"

        if stage == "script":
            states["PrepareScript"] = task(
                {
                    "action": "prepare_script",
                    "run_id.$": "$.run_context.run_id",
                    "model.$": "$.model",
                },
                prepared,
                "CheckScriptPreparation",
                "StageInvocationFailed",
            )

        states[f"Check{title}Preparation"] = choice(
            [
                {
                    "And": [
                        matches(
                            f"{prepared}.status",
                            preparation_status,
                        ),
                        same_run(prepared),
                    ],
                    "Next": f"Submit{title}",
                }
            ],
            "UnexpectedStageResult",
        )

        states[f"Submit{title}"] = task(
            {
                "action": f"submit_{stage}",
                "run_id.$": "$.run_context.run_id",
                "confirm_paid_generation.$": (
                    "$.confirm_paid_generation"
                ),
            },
            submitted,
            f"Check{title}Submission",
            "SubmissionOutcomeUncertain",
        )

        states[f"Check{title}Submission"] = choice(
            [
                {
                    "And": [
                        matches(
                            f"{submitted}.status",
                            "initial_response_preserved",
                        ),
                        same_run(submitted),
                        {
                            "Variable": (
                                f"{submitted}.generation_requested"
                            ),
                            "BooleanEquals": True,
                        },
                        {
                            "Variable": f"{submitted}.response_id",
                            "IsString": True,
                        },
                        {
                            "Not": matches(
                                f"{submitted}.response_id", ""
                            ),
                        },
                    ],
                    "Next": f"Initialize{title}Polling",
                },
                {
                    **matches(
                        f"{submitted}.status",
                        "existing_claim_submission_blocked",
                    ),
                    "Next": "ExistingSubmissionClaim",
                },
            ],
            "SubmissionOutcomeUncertain",
        )

        states[f"Initialize{title}Polling"] = {
            "Type": "Pass",
            "Result": {"count": 0},
            "ResultPath": poll,
            "Next": f"WaitFor{title}",
        }

        states[f"WaitFor{title}"] = {
            "Type": "Wait",
            "Seconds": WAIT_SECONDS,
            "Next": f"Retrieve{title}",
        }

        states[f"Retrieve{title}"] = task(
            {
                "action": f"retrieve_{stage}",
                "run_id.$": "$.run_context.run_id",
            },
            retrieved,
            f"Check{title}Identity",
            "StageInvocationFailed",
        )

        states[f"Check{title}Identity"] = choice(
            [
                {
                    "And": [
                        same_run(retrieved),
                        {
                            "Variable": f"{retrieved}.response_id",
                            "StringEqualsPath": (
                                f"{submitted}.response_id"
                            ),
                        },
                    ],
                    "Next": f"Check{title}Retrieval",
                }
            ],
            "UnexpectedStageResult",
        )

        states[f"Check{title}Retrieval"] = choice(
            [
                {
                    **matches(
                        f"{retrieved}.status",
                        "completed_response_preserved",
                    ),
                    "Next": f"Validate{title}",
                },
                {
                    **matches(
                        f"{retrieved}.status",
                        "generation_pending",
                    ),
                    "Next": f"Count{title}Poll",
                },
                {
                    **matches(
                        f"{retrieved}.status",
                        "generation_unsuccessful",
                    ),
                    "Next": "GenerationUnsuccessful",
                },
            ],
            "UnexpectedStageResult",
        )

        states[f"Count{title}Poll"] = {
            "Type": "Pass",
            "Parameters": {
                "count.$": f"States.MathAdd({poll}.count, 1)"
            },
            "ResultPath": poll,
            "Next": f"Check{title}PollingLimit",
        }

        states[f"Check{title}PollingLimit"] = choice(
            [
                {
                    "Variable": f"{poll}.count",
                    "NumericGreaterThanEquals": MAX_POLLS,
                    "Next": "PollingLimitReached",
                }
            ],
            f"WaitFor{title}",
        )

        states[f"Validate{title}"] = task(
            {
                "action": f"validate_{stage}",
                "run_id.$": "$.run_context.run_id",
                "snapshot_filename.$": (
                    f"{retrieved}.snapshot_filename"
                ),
            },
            validated,
            f"Check{title}Acceptance",
            "StageInvocationFailed",
        )

        states[f"Check{title}Acceptance"] = choice(
            [
                {
                    "And": [
                        matches(
                            f"{validated}.status",
                            validation_status,
                        ),
                        matches(
                            f"{validated}.acceptance_status",
                            "accepted",
                        ),
                        same_run(validated),
                        {
                            "Variable": f"{validated}.response_id",
                            "StringEqualsPath": (
                                f"{retrieved}.response_id"
                            ),
                        },
                        {
                            "Variable": (
                                f"{validated}.snapshot_filename"
                            ),
                            "StringEqualsPath": (
                                f"{retrieved}.snapshot_filename"
                            ),
                        },
                    ],
                    "Next": next_stage,
                }
            ],
            "UnexpectedStageResult",
        )

    failures = {
        "InvalidInput": (
            "Authorize both text generations and choose exactly one "
            "of prior_run_id or no_prior_state=true."
        ),
        "StageInvocationFailed": (
            "Preparation, retrieval, or validation failed. "
            "Inspect saved evidence before recovery."
        ),
        "UnexpectedStageResult": (
            "A stage returned an unexpected result or identity."
        ),
        "SubmissionOutcomeUncertain": (
            "Submission did not return confirmed preservation. "
            "Do not automatically resubmit."
        ),
        "ExistingSubmissionClaim": (
            "An existing submission claim blocked this execution. "
            "Inspect recovery evidence; do not assume success."
        ),
        "GenerationUnsuccessful": (
            "Generation failed, was cancelled, or was incomplete. "
            "No automatic resubmission."
        ),
        "PollingLimitReached": (
            "Polling stopped after the configured limit. "
            "The remote generation may still be running."
        ),
    }

    for name, cause in failures.items():
        states[name] = {
            "Type": "Fail",
            "Error": name,
            "Cause": cause,
        }

    states["PrepareAudio"] = task(
        {
            "action": "prepare_audio",
            "run_id.$": "$.run_context.run_id",
        },
        "$.audio_prepare",
        "CheckAudioPreparation",
        "StageInvocationFailed",
    )

    states["CheckAudioPreparation"] = choice(
        [
            {
                "And": [
                    matches(
                        "$.audio_prepare.status",
                        "audio_job_prepared",
                    ),
                    same_run("$.audio_prepare"),
                    {
                        "Variable": "$.audio_prepare.generation_requested",
                        "BooleanEquals": False,
                    },
                ],
                "Next": "InitializeAudioExecution",
            }
        ],
        "UnexpectedStageResult",
    )

    states.update(build_audio_states(
        lambda_arn=LAMBDA_ARN,
        instance_id="i-0acf87a780fb97103",
    ))

    apply_shared_deadlines(states)

    return {
        "Comment": "Manual briefing-to-script-to-audio pipeline. No scheduling.",
        "QueryLanguage": "JSONPath",
        "StartAt": "InitializeWorkflowBudget",
        "TimeoutSeconds": 3600,
        "States": states,
    }


def main():
    output = Path(__file__).with_name("text-workflow.asl.json")
    output.write_text(
        json.dumps(build_definition(), indent=2) + "\n",
        encoding="utf-8",
    )
    print(f"Workflow definition written to {output}")


if __name__ == "__main__":
    main()