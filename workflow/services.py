"""Central state guards for the V1 task lifecycle.

These functions intentionally contain no imports from contentops or releasegate.
They accept the related immutable facts and validate them by their stable contract,
so the owning application can call the guard while holding its own transaction.
"""

from __future__ import annotations

from django.core.exceptions import ObjectDoesNotExist, ValidationError

from workflow.exceptions import CheckGateRejected, IllegalTaskTransition


def require_task_state(task, *, action: str, allowed_states: set[str]) -> None:
    if task.current_state not in allowed_states:
        allowed = ", ".join(sorted(allowed_states))
        raise IllegalTaskTransition(
            f"{action} requires task state {allowed}; current state is {task.current_state}."
        )


def guard_check_run(task, check_kind: str) -> None:
    """Fail closed when a DoR/DoD check is attempted in the wrong phase."""

    if check_kind == "DOR":
        require_task_state(task, action="DoR check", allowed_states={"DRAFT"})
        return
    if check_kind == "DOD":
        require_task_state(task, action="DoD check", allowed_states={"IN_PROGRESS"})
        return
    raise ValidationError({"check_kind": "Unknown task check kind."})


def _latest_submission(task):
    manager = getattr(task, "submissions", None)
    if manager is None:
        return None
    return manager.order_by("-submission_number").first()


def _final_review(submission):
    try:
        return submission.final_review
    except ObjectDoesNotExist:
        return None


def _is_withdrawn(submission) -> bool:
    manager = getattr(submission, "withdrawal_events", None)
    if manager is None:
        return False
    return manager.filter(
        event_type__in={"SUBMISSION_WITHDRAWN", "SUBMISSION_ABANDONED"}
    ).exists()


def _is_owner_self_approval(review, submission) -> bool:
    """Return whether a self-authored approval uses the explicit Owner path."""

    return bool(
        review is not None
        and review.reviewer_principal_id == submission.submitted_by_principal_id
        and review.decision == "APPROVED"
        and review.reviewer_acting_role == "OWNER"
        and getattr(review.reviewer_principal, "role", None) == "OWNER"
    )


def guard_submission(task, *, submission=None, dod_check_run=None) -> None:
    """Validate the exact immutable facts required to seal/accept a submission."""

    require_task_state(task, action="Content submission", allowed_states={"IN_PROGRESS"})
    if submission is None and dod_check_run is None:
        submission = _latest_submission(task)
    run = dod_check_run or getattr(submission, "dod_check_run", None)
    if run is None:
        raise CheckGateRejected("Submission requires an exact completed DoD run.")
    if (
        run.task_id != task.pk
        or run.check_kind != "DOD"
        or run.status != "COMPLETED"
        or run.aggregate_result != "PASS"
        or run.actual_criterion_count != run.expected_criterion_count
        or run.contract_version_id != task.contract_version_id
    ):
        raise CheckGateRejected("Submission requires the task's exact complete passing DoD run.")
    latest_run = task.check_runs.filter(check_kind="DOD", status="COMPLETED").order_by(
        "-attempt_number"
    ).first()
    if latest_run is None or latest_run.pk != run.pk:
        raise CheckGateRejected("Submission must use the latest completed DoD run.")
    if submission is not None:
        if submission.task_id != task.pk or submission.dod_check_run_id != run.pk:
            raise CheckGateRejected("Submission and DoD facts do not belong to the same task.")
        if submission.expected_task_version != task.state_version:
            raise CheckGateRejected("Submission was not sealed against the current task version.")


def guard_review(task, *, submission) -> None:
    """Validate the exact submission before a human review is recorded."""

    require_task_state(task, action="Human review", allowed_states={"UNDER_REVIEW"})
    latest = _latest_submission(task)
    if latest is None or latest.pk != submission.pk or submission.task_id != task.pk:
        raise CheckGateRejected("Review requires the task's latest exact sealed submission.")
    if _is_withdrawn(submission):
        raise CheckGateRejected(
            "A withdrawn or abandoned submission cannot receive a review decision."
        )


def guard_release_gate(task, *, submission, review_decision) -> None:
    """Validate task, submission and final human approval before gate evaluation."""

    require_task_state(task, action="Release gate", allowed_states={"APPROVED"})
    latest = _latest_submission(task)
    if latest is None or latest.pk != submission.pk or submission.task_id != task.pk:
        raise CheckGateRejected("Release gate requires the latest exact task submission.")
    if (
        review_decision is None
        or review_decision.submission_id != submission.pk
        or review_decision.decision != "APPROVED"
    ):
        raise CheckGateRejected("Release gate requires the exact final APPROVED human review.")
    if (
        review_decision.reviewer_principal_id == submission.submitted_by_principal_id
        and not _is_owner_self_approval(review_decision, submission)
    ):
        raise CheckGateRejected(
            "Only an explicit Owner self-approval may pass the release gate."
        )


def guard_manual_publication(task, *, publication) -> None:
    """Validate the task boundary before recording external publication proof."""

    require_task_state(task, action="Manual publication", allowed_states={"APPROVED"})
    if publication is None or publication.submission.task_id != task.pk:
        raise CheckGateRejected("Publication intent does not belong to this task.")
    if publication.status != "READY_FOR_MANUAL_PUBLISH":
        raise CheckGateRejected("Manual publication requires an exact current READY gate.")


def guard_transition_prerequisites(task, to_state: str) -> None:
    """Enforce cross-domain facts before projecting a publish-adjacent task state."""

    if to_state == "SUBMITTED":
        submission = _latest_submission(task)
        if submission is None:
            raise CheckGateRejected("SUBMITTED requires an exact sealed TaskSubmission.")
        guard_submission(task, submission=submission)
        return

    if to_state == "UNDER_REVIEW":
        submission = _latest_submission(task)
        if submission is None or submission.task_id != task.pk:
            raise CheckGateRejected("UNDER_REVIEW requires the latest exact TaskSubmission.")
        if submission.expected_task_version != task.state_version - 1:
            raise CheckGateRejected("The latest TaskSubmission does not match the preceding task version.")
        if _final_review(submission) is not None:
            raise CheckGateRejected("A review recorded before UNDER_REVIEW is invalid.")
        if _is_withdrawn(submission):
            raise CheckGateRejected("A withdrawn submission cannot return to UNDER_REVIEW.")
        return

    if to_state in {"APPROVED", "HUMAN_REWORK"}:
        submission = _latest_submission(task)
        review = _final_review(submission) if submission is not None else None
        required_decision = "APPROVED" if to_state == "APPROVED" else "CHANGES_REQUESTED"
        if (
            submission is None
            or _is_withdrawn(submission)
            or review is None
            or review.submission_id != submission.pk
            or review.decision != required_decision
            or review.expected_task_version != task.state_version
            or (
                review.reviewer_principal_id == submission.submitted_by_principal_id
                and not _is_owner_self_approval(review, submission)
            )
        ):
            raise CheckGateRejected(
                f"{to_state} requires the latest submission's exact {required_decision} human review."
            )
        return

    if to_state == "DONE":
        submission = _latest_submission(task)
        publication = (
            submission.publications.order_by("-created_at").first() if submission is not None else None
        )
        if publication is None or publication.status != "MANUAL_PUBLISHED_RECORDED":
            raise CheckGateRejected("DONE requires immutable proof of the manual publication.")
