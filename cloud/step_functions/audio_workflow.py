"""Build audio orchestration states without contacting AWS."""

from cloud.step_functions.audio_cleanup import build_unacknowledged_cleanup_states


def build_audio_states(*, lambda_arn, instance_id):
    states = build_unacknowledged_cleanup_states(
        lambda_arn=lambda_arn, instance_id=instance_id,
    )
    owned = {
        "run_id.$": "$.run_context.run_id",
        "execution_arn.$": "$.audio_execution.arn",
        "owner_token.$": "$.audio_worker.owner_token",
        "lock_etag.$": "$.audio_worker.lock_etag",
    }

    def task(name, action, next_state, failure, *, result="$.audio_stage_result",
             cleanup=False, extra=None):
        states[name] = {
            "Type": "Task", "Resource": lambda_arn,
            "Parameters": {"action": action, **owned, **(extra or {})},
            "ResultPath": result, "TimeoutSeconds": 125,
            "Catch": [{
                "ErrorEquals": ["States.ALL"],
                "ResultPath": "$.audio_cleanup_error" if cleanup else "$.audio_failure",
                "Next": failure,
            }],
            "Next": next_state,
        }

    def route(name, routes, default, *, path="$.audio_stage_result", generation=False):
        rules = []
        for status, target in routes.items():
            checks = [
                {"Variable": f"{path}.status", "StringEquals": status},
                {"Variable": f"{path}.run_id", "StringEqualsPath": "$.run_context.run_id"},
                {"Variable": f"{path}.execution_arn", "StringEqualsPath": "$.audio_execution.arn"},
                {"Variable": f"{path}.instance_id", "StringEquals": instance_id},
            ]
            if generation is not None:
                checks.append({
                    "Variable": f"{path}.generation_requested",
                    "BooleanEquals": generation,
                })
            rules.append({"And": checks, "Next": target})
        states[name] = {"Type": "Choice", "Choices": rules, "Default": default}

    def polling(label, operation, limit, failure):
        states[f"Initialize{label}Polling"] = {
            "Type": "Pass", "Result": {"count": 0},
            "ResultPath": f"$.{label}_poll", "Next": operation,
        }
        states[f"Count{label}Poll"] = {
            "Type": "Pass",
            "Parameters": {"count.$": f"States.MathAdd($.{label}_poll.count, 1)"},
            "ResultPath": f"$.{label}_poll", "Next": f"Check{label}PollLimit",
        }
        states[f"Check{label}PollLimit"] = {
            "Type": "Choice", "Choices": [{
                "Variable": f"$.{label}_poll.count",
                "NumericGreaterThanEquals": limit, "Next": failure,
            }], "Default": f"WaitFor{label}",
        }
        states[f"WaitFor{label}"] = {
            "Type": "Wait", "Seconds": 30, "Next": operation,
        }

    states["InitializeAudioExecution"] = {
        "Type": "Pass", "Parameters": {"arn.$": "$$.Execution.Id"},
        "ResultPath": "$.audio_execution", "Next": "AcquireAudioWorker",
    }
    task("AcquireAudioWorker", "acquire_audio_worker", "CheckAudioOwnership",
         "AudioOwnershipUnresolved", result="$.audio_worker")
    states["AcquireAudioWorker"]["Parameters"] = {
        "action": "acquire_audio_worker",
        "run_id.$": "$.run_context.run_id",
        "execution_arn.$": "$.audio_execution.arn",
    }
    route("CheckAudioOwnership", {
        "audio_worker_acquired": "CheckAudioOwnershipTokens",
    }, "AudioOwnershipUnresolved", path="$.audio_worker")

    states["CheckAudioOwnershipTokens"] = {
        "Type": "Choice", "Choices": [{
            "And": [
                condition
                for field in ("owner_token", "lock_etag")
                for condition in (
                    {"Variable": f"$.audio_worker.{field}", "IsPresent": True},
                    {"Variable": f"$.audio_worker.{field}", "IsString": True},
                    {"Not": {
                        "Variable": f"$.audio_worker.{field}",
                        "StringEquals": "",
                    }},
                )
            ], "Next": "StartAudioWorker",
        }], "Default": "AudioOwnershipUnresolved",
    }
    task("StartAudioWorker", "start_audio_worker", "CheckAudioWorkerStart",
         "AudioStartupOutcomeUnresolved")
    route("CheckAudioWorkerStart", {
        "audio_instance_start_requested": "InitializeAudioConnectionPolling",
    }, "AudioStartupOutcomeUnresolved")

    pre_cleanup = "BeginUnacknowledgedAudioCleanup"
    polling("AudioConnection", "CheckAudioConnection", 20, "RecordAudioStageFailure")
    task("CheckAudioConnection", "check_audio_worker_connection",
         "RouteAudioConnection", pre_cleanup)
    route("RouteAudioConnection", {
        "audio_worker_connection_ready": "SubmitAudioSafeguards",
        "audio_worker_connection_pending": "CountAudioConnectionPoll",
    }, "RecordAudioStageFailure")

    for label, submit, submitted, retrieve, pending, completed, next_state, generation in (
        ("AudioSafeguards", "submit_audio_safeguard_check", "audio_safeguard_check_submitted",
         "retrieve_audio_safeguard_check", "audio_safeguard_check_pending",
         "audio_safeguards_verified", "PrepareAudioHandoff", False),
        ("AudioHandoff", "submit_audio_handoff", "audio_handoff_submitted",
         "retrieve_audio_handoff", "audio_handoff_pending",
         "audio_job_handed_off", "SubmitAudioServiceStart", False),
        ("AudioServiceStart", "submit_audio_service_start", "audio_service_start_submitted",
         "retrieve_audio_service_start", "audio_service_start_pending",
         "audio_service_start_acknowledged", "InitializeAudioCompletionPolling", True),
    ):
        task(f"Submit{label}", submit, f"Check{label}Submission", pre_cleanup)
        route(f"Check{label}Submission", {
            submitted: f"Initialize{label}Polling",
        }, "RecordAudioStageFailure", generation=generation)
        polling(label, f"Retrieve{label}", 20, "RecordAudioStageFailure")
        task(f"Retrieve{label}", retrieve, f"Check{label}Result", pre_cleanup)
        route(f"Check{label}Result", {
            pending: f"Count{label}Poll", completed: next_state,
        }, "RecordAudioStageFailure", generation=generation)

    task("PrepareAudioHandoff", "prepare_audio_handoff",
         "CheckAudioHandoffPreparation", pre_cleanup)
    route("CheckAudioHandoffPreparation", {
        "audio_handoff_prepared": "SubmitAudioHandoff",
    }, "RecordAudioStageFailure")
    states["RecordAudioStageFailure"] = {
        "Type": "Pass", "Parameters": {
            "Error": "AudioStageUnsuccessfulOrUnexpected",
            "result.$": "$.audio_stage_result",
        }, "ResultPath": "$.audio_failure", "Next": pre_cleanup,
    }

    ack_cleanup = "InitializeAcknowledgedAudioShutdownPolling"
    polling("AudioCompletion", "MonitorAudioCompletion", 121,
            "RecordAudioCompletionFailure")
    task("MonitorAudioCompletion", "monitor_audio_completion",
         "CheckAudioCompletionResult", ack_cleanup, result="$.audio_completion")
    route("CheckAudioCompletionResult", {
        "audio_completion_verified": ack_cleanup,
        "audio_completion_waiting": "CountAudioCompletionPoll",
    }, "RecordAudioCompletionFailure", path="$.audio_completion", generation=None)
    states["RecordAudioCompletionFailure"] = {
        "Type": "Pass", "Parameters": {
            "Error": "AudioCompletionUnsuccessfulOrUnexpected",
            "result.$": "$.audio_completion",
        }, "ResultPath": "$.audio_failure", "Next": ack_cleanup,
    }

    polling("AcknowledgedAudioShutdown", "ObserveAcknowledgedAudioShutdown",
            21, "AudioShutdownDeadlineExceeded")
    states[ack_cleanup]["Parameters"] = {
        "count": 0, "started_at.$": "$$.State.EnteredTime",
        "stop_attempted": False,
    }
    del states[ack_cleanup]["Result"]
    task("ObserveAcknowledgedAudioShutdown", "monitor_audio_worker_shutdown",
         "CheckAcknowledgedAudioShutdown", "AudioCleanupUnresolved", cleanup=True,
         result="$.audio_shutdown", extra={
             "shutdown_wait_started_at.$": "$.AcknowledgedAudioShutdown_poll.started_at",
         })
    route("CheckAcknowledgedAudioShutdown", {
        "audio_worker_shutdown_confirmed": "ReleaseAcknowledgedAudioWorker",
        "audio_worker_shutdown_pending": "ChooseAcknowledgedAudioStop",
        "audio_worker_shutdown_deadline_exceeded": "AudioShutdownDeadlineExceeded",
    }, "AudioCleanupUnresolved", path="$.audio_shutdown")
    states["ChooseAcknowledgedAudioStop"] = {
        "Type": "Choice", "Choices": [{
            "And": [
                {"Variable": "$.audio_shutdown.instance_state", "StringEquals": "running"},
                {
                    "Variable": "$.AcknowledgedAudioShutdown_poll.stop_attempted",
                    "BooleanEquals": False,
                },
            ], "Next": "MarkAcknowledgedAudioStopAttempt",
        }], "Default": "CountAcknowledgedAudioShutdownPoll",
    }
    states["MarkAcknowledgedAudioStopAttempt"] = {
        "Type": "Pass", "Result": True,
        "ResultPath": "$.AcknowledgedAudioShutdown_poll.stop_attempted",
        "Next": "StopAcknowledgedAudioWorker",
    }
    task("StopAcknowledgedAudioWorker", "submit_audio_worker_stop",
         "CheckAcknowledgedAudioStop", "AudioCleanupUnresolved",
         cleanup=True, result="$.audio_stop")
    route("CheckAcknowledgedAudioStop", {
        status: "CountAcknowledgedAudioShutdownPoll" for status in (
            "audio_worker_stop_requested",
            "audio_worker_shutdown_pending",
            "audio_worker_shutdown_confirmed",
        )
    }, "AudioCleanupUnresolved", path="$.audio_stop")
    states["CountAcknowledgedAudioShutdownPoll"]["Parameters"].update({
        "started_at.$": "$.AcknowledgedAudioShutdown_poll.started_at",
        "stop_attempted.$": "$.AcknowledgedAudioShutdown_poll.stop_attempted",
    })
    task("ReleaseAcknowledgedAudioWorker", "release_acknowledged_audio_worker",
         "CheckAcknowledgedAudioRelease", "AudioCleanupUnresolved",
         cleanup=True, result="$.audio_release")
    route("CheckAcknowledgedAudioRelease", {
        "audio_worker_released": "ChooseFinalAudioOutcome",
    }, "AudioCleanupUnresolved", path="$.audio_release")
    states["ChooseFinalAudioOutcome"] = {
        "Type": "Choice", "Choices": [{
            "And": [
                {"Variable": "$.audio_failure", "IsPresent": False},
                {
                    "Variable": "$.audio_completion.status",
                    "StringEquals": "audio_completion_verified",
                },
            ], "Next": "AudioPipelineSucceeded",
        }], "Default": "AudioFailedAfterCleanup",
    }
    states["AudioPipelineSucceeded"] = {"Type": "Succeed"}
    for name, cause in {
        "AudioOwnershipUnresolved": (
            "Worker busy or ownership uncertain. Inspect evidence; do not start or release."
        ),
        "AudioFailedAfterCleanup": (
            "Audio failed; worker stopped and ownership released. Inspect $.audio_failure."
        ),
    }.items():
        states[name] = {"Type": "Fail", "Error": name, "Cause": cause}
    return states