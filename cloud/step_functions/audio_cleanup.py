"""Build cleanup states for failures before acknowledged audio service startup."""


def build_unacknowledged_cleanup_states(*, lambda_arn, instance_id):
    """Require owned worker context and a confirmed EC2 start before entry.

    Preserve the original failure under $.audio_failure. This fragment never
    releases ownership, retries a stop request, or submits an SSM command.
    """
    owned = {
        "run_id.$": "$.run_context.run_id",
        "execution_arn.$": "$$.Execution.Id",
        "owner_token.$": "$.audio_worker.owner_token",
        "lock_etag.$": "$.audio_worker.lock_etag",
    }

    def invoke(action, result_path, next_state, **extra):
        return {
            "Type": "Task",
            "Resource": lambda_arn,
            "Parameters": {"action": action, **owned, **extra},
            "ResultPath": result_path,
            "TimeoutSeconds": 125,
            "Catch": [{
                "ErrorEquals": ["States.ALL"],
                "ResultPath": "$.audio_cleanup_error",
                "Next": "AudioCleanupUnresolved",
            }],
            "Next": next_state,
        }

    def result_rule(path, status, next_state):
        return {
            "And": [
                {"Variable": f"{path}.status", "StringEquals": status},
                {
                    "Variable": f"{path}.run_id",
                    "StringEqualsPath": "$.run_context.run_id",
                },
                {
                    "Variable": f"{path}.execution_arn",
                    "StringEqualsPath": "$.audio_worker.execution_arn",
                },
                {
                    "Variable": f"{path}.instance_id",
                    "StringEquals": instance_id,
                },
                {
                    "Variable": f"{path}.generation_requested",
                    "BooleanEquals": False,
                },
            ],
            "Next": next_state,
        }

    return {
        "BeginUnacknowledgedAudioCleanup": {
            "Type": "Pass",
            "Parameters": {
                "started_at.$": "$$.State.EnteredTime",
                "stop_attempted": False,
                "poll_count": 0,
            },
            "ResultPath": "$.audio_cleanup",
            "Next": "ObserveUnacknowledgedAudioShutdown",
        },
        "ObserveUnacknowledgedAudioShutdown": invoke(
            "monitor_audio_worker_shutdown",
            "$.audio_cleanup_observation",
            "CheckUnacknowledgedAudioShutdown",
            **{"shutdown_wait_started_at.$": "$.audio_cleanup.started_at"},
        ),
        "CheckUnacknowledgedAudioShutdown": {
            "Type": "Choice",
            "Choices": [
                result_rule(
                    "$.audio_cleanup_observation",
                    "audio_worker_shutdown_confirmed",
                    "AudioFailedWorkerStoppedOwnershipRetained",
                ),
                result_rule(
                    "$.audio_cleanup_observation",
                    "audio_worker_shutdown_deadline_exceeded",
                    "AudioShutdownDeadlineExceeded",
                ),
                result_rule(
                    "$.audio_cleanup_observation",
                    "audio_worker_shutdown_pending",
                    "ChooseUnacknowledgedAudioStop",
                ),
            ],
            "Default": "AudioCleanupUnresolved",
        },
        "ChooseUnacknowledgedAudioStop": {
            "Type": "Choice",
            "Choices": [{
                "And": [
                    {
                        "Variable": "$.audio_cleanup_observation.instance_state",
                        "StringEquals": "running",
                    },
                    {
                        "Variable": "$.audio_cleanup.stop_attempted",
                        "BooleanEquals": False,
                    },
                ],
                "Next": "MarkUnacknowledgedAudioStopAttempt",
            }],
            "Default": "CountUnacknowledgedAudioShutdownPoll",
        },
        "MarkUnacknowledgedAudioStopAttempt": {
            "Type": "Pass",
            "Result": True,
            "ResultPath": "$.audio_cleanup.stop_attempted",
            "Next": "StopUnacknowledgedAudioWorker",
        },
        "StopUnacknowledgedAudioWorker": invoke(
            "submit_audio_worker_stop",
            "$.audio_cleanup_stop",
            "CheckUnacknowledgedAudioStop",
        ),
        "CheckUnacknowledgedAudioStop": {
            "Type": "Choice",
            "Choices": [
                result_rule(
                    "$.audio_cleanup_stop", status,
                    "CountUnacknowledgedAudioShutdownPoll",
                )
                for status in (
                    "audio_worker_stop_requested",
                    "audio_worker_shutdown_pending",
                    "audio_worker_shutdown_confirmed",
                )
            ],
            "Default": "AudioCleanupUnresolved",
        },
        "CountUnacknowledgedAudioShutdownPoll": {
            "Type": "Pass",
            "Parameters": {
                "started_at.$": "$.audio_cleanup.started_at",
                "stop_attempted.$": "$.audio_cleanup.stop_attempted",
                "poll_count.$": "States.MathAdd($.audio_cleanup.poll_count, 1)",
            },
            "ResultPath": "$.audio_cleanup",
            "Next": "CheckUnacknowledgedAudioShutdownPollLimit",
        },
        "CheckUnacknowledgedAudioShutdownPollLimit": {
            "Type": "Choice",
            "Choices": [{
                "Variable": "$.audio_cleanup.poll_count",
                "NumericGreaterThanEquals": 21,
                "Next": "AudioShutdownDeadlineExceeded",
            }],
            "Default": "WaitForUnacknowledgedAudioShutdown",
        },
        "WaitForUnacknowledgedAudioShutdown": {
            "Type": "Wait",
            "Seconds": 30,
            "Next": "ObserveUnacknowledgedAudioShutdown",
        },
        "AudioFailedWorkerStoppedOwnershipRetained": {
            "Type": "Fail",
            "Error": "AudioFailedWorkerStoppedOwnershipRetained",
            "Cause": (
                "Worker observed stopped after audio failure. Ownership retained; "
                "reconcile outstanding commands. Original failure: $.audio_failure."
            ),
        },
        "AudioShutdownDeadlineExceeded": {
            "Type": "Fail",
            "Error": "AudioShutdownDeadlineExceeded",
            "Cause": (
                "Shutdown observation limit reached. Ownership retained. "
                "Inspect $.audio_failure and saved request evidence."
            ),
        },
        "AudioCleanupUnresolved": {
            "Type": "Fail",
            "Error": "AudioCleanupUnresolved",
            "Cause": (
                "Cleanup failed or returned unexpected evidence. Ownership "
                "retained. Inspect $.audio_failure and $.audio_cleanup_error."
            ),
        },
        "AudioStartupOutcomeUnresolved": {
            "Type": "Fail",
            "Error": "AudioStartupOutcomeUnresolved",
            "Cause": (
                "EC2 startup outcome unresolved. Do not restart, stop, or release "
                "automatically. Reconcile $.audio_failure and saved startup evidence."
            ),
        },
    }