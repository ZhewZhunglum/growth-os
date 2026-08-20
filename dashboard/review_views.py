from __future__ import annotations

import hashlib
import tempfile
import uuid
from pathlib import Path

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied, SuspiciousOperation, ValidationError
from django.core.files.storage import default_storage
from django.db import transaction
from django.http import FileResponse, Http404, HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_GET, require_POST

from accounts.authorization import require_authorization, resolve_authorization
from accounts.models import PermissionGrant, Principal
from contentops.models import ContentAssetVersion, ReviewDecision, TaskSubmission
from dashboard.review_forms import (
    CompleteTaskForm,
    PublicationProofForm,
    ReleaseGateForm,
    ReviewDecisionForm,
)
from dashboard.storage_cleanup import StoredObjectWrite
from releasegate.models import ChannelAccount, Publication, PublicationEvent, RuntimeEnvironment
from releasegate.services import orchestrate_v1_release_gate, record_manual_publication_proof
from workflow.models import Task


MAX_PROOF_BYTES = 25 * 1024 * 1024
ALLOWED_PROOF_TYPES = {"application/pdf", "image/jpeg", "image/png", "image/webp"}
SAFE_INLINE_ASSET_TYPES = ALLOWED_PROOF_TYPES | {"text/plain"}


def _proof_mime_from_header(header: bytes) -> str | None:
    if header.startswith(b"%PDF-"):
        return "application/pdf"
    if header.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if header.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if len(header) >= 12 and header[:4] == b"RIFF" and header[8:12] == b"WEBP":
        return "image/webp"
    return None


def _subcommand(root: uuid.UUID, label: str) -> uuid.UUID:
    return uuid.uuid5(root, f"growth-os:dashboard-review-release:{label}")


def _validation_text(error: ValidationError) -> str:
    return " ".join(error.messages) if error.messages else "操作未通过校验，请刷新后重试。"


def _product_decision(user: Principal, task: Task, action: str):
    return resolve_authorization(
        principal=user,
        acting_role=user.role,
        action=action,
        scope_kind=PermissionGrant.ScopeKind.PRODUCT,
        product=task.product,
    )


def _require_product_grant(user: Principal, task: Task, action: str):
    return require_authorization(
        principal=user,
        acting_role=user.role,
        action=action,
        scope_kind=PermissionGrant.ScopeKind.PRODUCT,
        product=task.product,
    )


def _publish_decision(user: Principal, task: Task, account: ChannelAccount):
    return resolve_authorization(
        principal=user,
        acting_role=user.role,
        action=PermissionGrant.Action.PUBLISH,
        scope_kind=PermissionGrant.ScopeKind.ACCOUNT,
        product=task.product,
        platform_code=account.platform_code,
        account_ref=account.account_code,
    )


def _allowed_accounts(user: Principal, task: Task):
    account_ids = [
        account.pk
        for account in ChannelAccount.objects.filter(status=ChannelAccount.Status.ACTIVE).order_by(
            "platform_code", "account_code"
        )
        if _publish_decision(user, task, account).allowed
    ]
    return ChannelAccount.objects.filter(pk__in=account_ids).order_by("platform_code", "account_code")


def _allowed_environments(accounts):
    return RuntimeEnvironment.objects.filter(
        status=RuntimeEnvironment.Status.ACTIVE,
        account_bindings__channel_account__in=accounts,
    ).distinct().order_by("environment_code")


def _can_complete(user: Principal, task: Task) -> bool:
    if not _product_decision(user, task, PermissionGrant.Action.COMPLETE_TASK).allowed:
        return False
    submission = task.submissions.order_by("-submission_number").first()
    if submission is None:
        return False
    latest_publication = submission.publications.order_by("-created_at", "-id").first()
    return bool(
        latest_publication
        and latest_publication.status == Publication.Status.MANUAL_PUBLISHED_RECORDED
    )


def _latest_submission(task: Task) -> TaskSubmission:
    submission = task.submissions.select_related(
        "primary_asset_version__content_asset",
        "dod_check_run",
    ).order_by("-submission_number").first()
    if submission is None:
        raise Http404("Task has no sealed submission.")
    return submission


def _can_review_submission(user: Principal, task: Task, submission: TaskSubmission) -> bool:
    """Return whether the user may independently review this exact submission."""

    return bool(
        submission.submitted_by_principal_id != user.pk
        and _product_decision(user, task, PermissionGrant.Action.REVIEW).allowed
    )


def _can_view_asset_version(
    user: Principal,
    task: Task,
    submission: TaskSubmission,
) -> bool:
    """Authorize a read of the exact immutable primary asset version."""

    if submission.primary_asset_version_id is None:
        return False
    if ReviewDecision.objects.filter(
        submission=submission,
        reviewer_principal=user,
    ).exists() and _product_decision(
        user, task, PermissionGrant.Action.REVIEW
    ).allowed:
        return True
    latest_submission = task.submissions.order_by("-submission_number").first()
    if latest_submission is None or latest_submission.pk != submission.pk:
        return False
    if task.current_state == Task.State.UNDER_REVIEW and _can_review_submission(
        user, task, submission
    ):
        return True
    if user.pk in {
        task.created_by_principal_id,
        task.current_assignee_principal_id,
    } and _product_decision(user, task, PermissionGrant.Action.EDIT).allowed:
        return True
    if task.current_state in {
        Task.State.APPROVED,
        Task.State.DONE,
    } and _product_decision(
        user, task, PermissionGrant.Action.COMPLETE_TASK
    ).allowed:
        return True
    if task.current_state == Task.State.APPROVED:
        publication = submission.publications.select_related(
            "current_gate__channel_account"
        ).filter(
            requested_by_principal=user,
            current_gate__isnull=False,
        ).order_by("-created_at", "-id").first()
        if publication and _publish_decision(
            user, task, publication.current_gate.channel_account
        ).allowed:
            return True
    return False


def _private_file_response(
    *,
    object_key: str,
    filename: str,
    mime_type: str,
    inline_types: set[str],
    expected_sha256: str,
    expected_size: int | None = None,
    maximum_size: int | None = None,
    detect_proof_mime: bool = False,
) -> FileResponse:
    """Verify and stream a private object without exposing a permanent URL."""

    if not object_key or not default_storage.exists(object_key):
        raise Http404("Stored object not found.")
    verified_file = tempfile.SpooledTemporaryFile(max_size=8 * 1024 * 1024, mode="w+b")
    digest = hashlib.sha256()
    byte_size = 0
    try:
        with default_storage.open(object_key, "rb") as stored_file:
            while True:
                chunk = stored_file.read(1024 * 1024)
                if not chunk:
                    break
                byte_size += len(chunk)
                if maximum_size is not None and byte_size > maximum_size:
                    raise SuspiciousOperation("STORED_OBJECT_EXCEEDS_VERIFIED_SIZE_LIMIT")
                if expected_size is not None and byte_size > expected_size:
                    raise SuspiciousOperation("STORED_OBJECT_SIZE_MISMATCH")
                digest.update(chunk)
                verified_file.write(chunk)
        if expected_size is not None and byte_size != expected_size:
            raise SuspiciousOperation("STORED_OBJECT_SIZE_MISMATCH")
        if not expected_sha256 or digest.hexdigest() != expected_sha256:
            raise SuspiciousOperation("STORED_OBJECT_SHA256_MISMATCH")
        verified_file.seek(0)
        if detect_proof_mime:
            detected_mime = _proof_mime_from_header(verified_file.read(12))
            verified_file.seek(0)
            if detected_mime is None:
                raise SuspiciousOperation("STORED_PROOF_TYPE_MISMATCH")
            mime_type = detected_mime
    except Exception:
        verified_file.close()
        raise
    safe_filename = Path(filename or object_key).name or "download.bin"
    response = FileResponse(
        verified_file,
        as_attachment=mime_type not in inline_types,
        filename=safe_filename,
        content_type=mime_type or "application/octet-stream",
    )
    response["Cache-Control"] = "private, no-store"
    response["X-Content-Type-Options"] = "nosniff"
    response["Cross-Origin-Resource-Policy"] = "same-origin"
    return response


@login_required
@require_GET
def asset_version_file(request: HttpRequest, asset_version_id) -> HttpResponse:
    asset_version = get_object_or_404(
        ContentAssetVersion.objects.select_related(
            "content_asset__task__product",
            "content_asset__task__created_by_principal",
            "content_asset__task__current_assignee_principal",
        ),
        pk=asset_version_id,
    )
    submissions = list(
        TaskSubmission.objects.select_related("task__product", "primary_asset_version")
        .filter(
            primary_asset_version=asset_version,
            task_id=asset_version.content_asset.task_id,
        )
        .order_by("id")[:2]
    )
    if len(submissions) != 1:
        raise Http404("Asset version not found.")
    submission = submissions[0]
    task = submission.task
    if not _can_view_asset_version(request.user, task, submission):
        raise Http404("Asset version not found.")
    return _private_file_response(
        object_key=asset_version.object_key,
        filename=asset_version.metadata.get("original_filename", "")
        if isinstance(asset_version.metadata, dict)
        else "",
        mime_type=asset_version.mime_type,
        inline_types=SAFE_INLINE_ASSET_TYPES,
        expected_sha256=asset_version.content_sha256,
        expected_size=asset_version.byte_size,
    )


@login_required
@require_GET
def publication_proof_file(request: HttpRequest, publication_event_id) -> HttpResponse:
    event = get_object_or_404(
        PublicationEvent.objects.select_related(
            "publication__submission__task__product",
            "publication__requested_by_principal",
            "publication__current_gate__channel_account",
        ),
        pk=publication_event_id,
        event_type=PublicationEvent.EventType.MANUAL_PUBLISHED_RECORDED,
    )
    if not _can_view_publication_proof_event(request.user, event):
        raise Http404("Publication proof not found.")
    if not event.proof_reference:
        raise Http404("Publication proof not found.")
    suffix = Path(event.proof_reference).suffix.lower()
    mime_type = {
        ".pdf": "application/pdf",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".png": "image/png",
        ".webp": "image/webp",
    }.get(suffix, "application/octet-stream")
    return _private_file_response(
        object_key=event.proof_reference,
        filename=Path(event.proof_reference).name,
        mime_type=mime_type,
        inline_types=ALLOWED_PROOF_TYPES,
        expected_sha256=event.proof_sha256,
        maximum_size=MAX_PROOF_BYTES,
        detect_proof_mime=True,
    )


def _can_view_publication_proof_event(
    user: Principal,
    event: PublicationEvent,
) -> bool:
    task = event.publication.submission.task
    current_gate = event.publication.current_gate
    may_complete = _product_decision(
        user,
        task,
        PermissionGrant.Action.COMPLETE_TASK,
    ).allowed
    may_publish = bool(
        current_gate
        and event.publication.requested_by_principal_id == user.pk
        and _publish_decision(
            user,
            task,
            current_gate.channel_account,
        ).allowed
    )
    return may_complete or may_publish


def _review_context(task: Task, *, form=None):
    submission = _latest_submission(task)
    return {
        "task": task,
        "submission": submission,
        "asset_version": submission.primary_asset_version,
        "review_form": form or ReviewDecisionForm(state_version=task.state_version),
    }


def _release_context(task: Task, user: Principal, *, gate_form=None, proof_form=None, done_form=None):
    submission = _latest_submission(task)
    accounts = _allowed_accounts(user, task)
    environments = _allowed_environments(accounts)
    publications = submission.publications.select_related(
        "current_gate__channel_account",
        "current_gate__runtime_environment",
        "requested_by_principal",
    ).prefetch_related("events").order_by("-created_at", "-id")
    ready_publications = publications.filter(
        status=Publication.Status.READY_FOR_MANUAL_PUBLISH,
        requested_by_principal=user,
    )
    initial_publication = ready_publications.first()
    can_publish = accounts.exists()
    publication_list = list(publications)
    readable_proof_event_ids = {
        event.pk
        for publication in publication_list
        for event in publication.events.all()
        if event.proof_reference and _can_view_publication_proof_event(user, event)
    }
    return {
        "task": task,
        "submission": submission,
        "publications": publication_list,
        "gate_form": gate_form or ReleaseGateForm(
            accounts=accounts,
            environments=environments,
            state_version=task.state_version,
        ),
        "proof_form": proof_form or (
            PublicationProofForm(
                publications=ready_publications,
                initial_publication=initial_publication,
            )
            if initial_publication and can_publish
            else None
        ),
        "done_form": done_form or (
            CompleteTaskForm(state_version=task.state_version) if _can_complete(user, task) else None
        ),
        "can_publish": can_publish,
        "can_view_asset": _can_view_asset_version(user, task, submission),
        "readable_proof_event_ids": readable_proof_event_ids,
    }


@login_required
def review_queue(request: HttpRequest) -> HttpResponse:
    tasks = []
    for task in (
        Task.objects.filter(current_state=Task.State.UNDER_REVIEW)
        .select_related("product", "contract_version", "current_assignee_principal")
        .prefetch_related("submissions")
        .order_by("updated_at", "title")
    ):
        submission = task.submissions.order_by("-submission_number").first()
        if submission is not None and _can_review_submission(request.user, task, submission):
            tasks.append(task)
    completed_reviews = (
        ReviewDecision.objects.filter(reviewer_principal=request.user)
        .select_related(
            "submission__task__product",
            "submission__primary_asset_version__content_asset",
        )
        .order_by("-decided_at", "-id")
    )
    return render(
        request,
        "dashboard/review_queue.html",
        {"tasks": tasks, "completed_reviews": completed_reviews},
    )


@login_required
def review_detail(request: HttpRequest, task_id) -> HttpResponse:
    task = get_object_or_404(
        Task.objects.select_related("product", "contract_version", "current_assignee_principal"),
        pk=task_id,
        current_state=Task.State.UNDER_REVIEW,
    )
    _require_product_grant(request.user, task, PermissionGrant.Action.REVIEW)
    submission = _latest_submission(task)
    if submission.submitted_by_principal_id == request.user.pk:
        raise PermissionDenied("SUBMITTER_CANNOT_REVIEW_OWN_SUBMISSION")
    return render(request, "dashboard/review_detail.html", _review_context(task))


@login_required
def review_history_detail(request: HttpRequest, review_id) -> HttpResponse:
    review = get_object_or_404(
        ReviewDecision.objects.select_related(
            "reviewer_principal",
            "submission__task__product",
            "submission__task__contract_version",
            "submission__primary_asset_version__content_asset",
        ),
        pk=review_id,
        reviewer_principal=request.user,
    )
    return render(
        request,
        "dashboard/review_history_detail.html",
        {
            "review": review,
            "submission": review.submission,
            "task": review.submission.task,
            "asset_version": review.submission.primary_asset_version,
            "can_view_asset": _can_view_asset_version(
                request.user,
                review.submission.task,
                review.submission,
            ),
        },
    )


@login_required
@require_POST
def review_action(request: HttpRequest, task_id) -> HttpResponse:
    task = get_object_or_404(Task.objects.select_related("product", "contract_version"), pk=task_id)
    review_grant = _require_product_grant(request.user, task, PermissionGrant.Action.REVIEW)
    # REVIEW and EDIT are deliberately independent grants.  The review fact
    # must not be written when the reviewer cannot project the Task state.
    edit_grant = _require_product_grant(request.user, task, PermissionGrant.Action.EDIT)
    form = ReviewDecisionForm(request.POST, state_version=task.state_version)
    if not form.is_valid():
        return render(
            request,
            "dashboard/review_detail.html",
            _review_context(task, form=form),
            status=400,
        )
    submission = _latest_submission(task)
    decision = form.cleaned_data["decision"]
    target = (
        Task.State.APPROVED
        if decision == ReviewDecision.Decision.APPROVED
        else Task.State.HUMAN_REWORK
    )
    root = form.cleaned_data["command_id"]
    try:
        with transaction.atomic():
            ReviewDecision.record_final(
                submission=submission,
                decision=decision,
                rationale=form.cleaned_data["rationale"],
                command_id=_subcommand(root, "review-decision"),
                expected_task_version=form.cleaned_data["expected_state_version"],
                reviewer_principal=request.user,
                acting_role=request.user.role,
                permission_grant=review_grant,
                recorded_by_principal=request.user,
            )
            Task.transition(
                task_id=task.pk,
                to_state=target,
                command_id=_subcommand(root, "review-transition"),
                expected_state_version=form.cleaned_data["expected_state_version"],
                actor_principal=request.user,
                acting_role=request.user.role,
                permission_grant=edit_grant,
                recorded_by_principal=request.user,
                reason=("Human review approved." if target == Task.State.APPROVED else "Human review requested changes."),
            )
        messages.success(
            request,
            "审核已通过，任务进入发布门禁。" if target == Task.State.APPROVED else "修改要求已保存，任务进入人工返工。",
        )
        return redirect("dashboard:review-queue")
    except ValidationError as error:
        messages.error(request, _validation_text(error))
    return redirect("dashboard:review-detail", task_id=task.pk)


@login_required
def release_queue(request: HttpRequest) -> HttpResponse:
    tasks = []
    for task in Task.objects.filter(current_state=Task.State.APPROVED).select_related(
        "product", "contract_version", "current_assignee_principal"
    ).order_by("updated_at", "title"):
        if _allowed_accounts(request.user, task).exists() or _can_complete(request.user, task):
            tasks.append(task)
    return render(request, "dashboard/release_queue.html", {"tasks": tasks})


@login_required
def release_detail(request: HttpRequest, task_id) -> HttpResponse:
    task = get_object_or_404(
        Task.objects.select_related("product", "contract_version", "current_assignee_principal"),
        pk=task_id,
        current_state=Task.State.APPROVED,
    )
    submission = _latest_submission(task)
    has_prior_release_fact = submission.publications.filter(
        requested_by_principal=request.user,
    ).exists()
    if (
        not _allowed_accounts(request.user, task).exists()
        and not _can_complete(request.user, task)
        and not has_prior_release_fact
    ):
        raise PermissionDenied("NO_RELEASE_QUEUE_PERMISSION")
    return render(request, "dashboard/release_detail.html", _release_context(task, request.user))


@login_required
@require_POST
def release_gate_action(request: HttpRequest, task_id) -> HttpResponse:
    task = get_object_or_404(
        Task.objects.select_related("product", "contract_version"),
        pk=task_id,
        current_state=Task.State.APPROVED,
    )
    accounts = _allowed_accounts(request.user, task)
    environments = _allowed_environments(accounts)
    form = ReleaseGateForm(
        request.POST,
        accounts=accounts,
        environments=environments,
        state_version=task.state_version,
    )
    if not form.is_valid():
        return render(
            request,
            "dashboard/release_detail.html",
            _release_context(task, request.user, gate_form=form),
            status=400,
        )
    if form.cleaned_data["expected_state_version"] != task.state_version:
        messages.error(request, "任务状态已经变化，请刷新后重新检查门禁。")
        return redirect("dashboard:release-detail", task_id=task.pk)
    try:
        result = orchestrate_v1_release_gate(
            task=task,
            submission=_latest_submission(task),
            publisher_principal=request.user,
            channel_account=form.cleaned_data["channel_account"],
            runtime_environment=form.cleaned_data["runtime_environment"],
            command_id=form.cleaned_data["command_id"],
        )
        if result.gate.outcome == "PASSED":
            messages.success(request, "门禁已通过。请你在平台上人工发布，然后回来上传证明。")
        else:
            messages.error(request, "门禁未通过，系统没有发布任何内容。请先处理页面显示的阻塞原因。")
    except ValidationError as error:
        messages.error(request, _validation_text(error))
    return redirect("dashboard:release-detail", task_id=task.pk)


def _hash_proof(uploaded) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    for chunk in uploaded.chunks():
        size += len(chunk)
        if size > MAX_PROOF_BYTES:
            raise ValidationError("发布证明超过 25 MB 上限。")
        digest.update(chunk)
    if not size:
        raise ValidationError("发布证明文件不能为空。")
    uploaded.seek(0)
    return digest.hexdigest(), size


def _valid_proof_signature(uploaded, mime_type: str) -> bool:
    header = uploaded.read(12)
    uploaded.seek(0)
    return _proof_mime_from_header(header) == mime_type


@login_required
@require_POST
def release_proof_action(request: HttpRequest, task_id) -> HttpResponse:
    task = get_object_or_404(
        Task.objects.select_related("product", "contract_version"),
        pk=task_id,
        current_state=Task.State.APPROVED,
    )
    submission = _latest_submission(task)
    # Include the terminal publication only so an identical command replay can
    # return its existing immutable proof without uploading another file.
    ready_publications = submission.publications.filter(
        status__in=[
            Publication.Status.READY_FOR_MANUAL_PUBLISH,
            Publication.Status.MANUAL_PUBLISHED_RECORDED,
        ],
        requested_by_principal=request.user,
    )
    form = PublicationProofForm(
        request.POST,
        request.FILES,
        publications=ready_publications,
    )
    if not form.is_valid():
        return render(
            request,
            "dashboard/release_detail.html",
            _release_context(task, request.user, proof_form=form),
            status=400,
        )
    publication = form.cleaned_data["publication"]
    uploaded = form.cleaned_data["proof_file"]
    mime_type = (uploaded.content_type or "application/octet-stream").lower()
    if mime_type not in ALLOWED_PROOF_TYPES:
        messages.error(request, "发布证明仅支持 PNG、JPEG、WebP 或 PDF。")
        return redirect("dashboard:release-detail", task_id=task.pk)
    try:
        proof_sha256, _size = _hash_proof(uploaded)
        if not _valid_proof_signature(uploaded, mime_type):
            raise ValidationError("发布证明的文件内容与所选图片/PDF类型不一致。")
        root = form.cleaned_data["command_id"]
        existing = PublicationEvent.objects.filter(command_id=root).first()
        if existing is not None:
            if (
                existing.publication_id != publication.pk
                or existing.event_type != PublicationEvent.EventType.MANUAL_PUBLISHED_RECORDED
                or existing.external_url != form.cleaned_data["external_url"]
                or existing.external_publication_id != form.cleaned_data["external_publication_id"]
                or existing.proof_sha256 != proof_sha256
            ):
                raise ValidationError("该 command_id 已用于不同的发布证明。")
            record_manual_publication_proof(
                publication=publication,
                publisher_principal=request.user,
                command_id=root,
                external_url=existing.external_url,
                external_publication_id=existing.external_publication_id,
                proof_reference=existing.proof_reference,
                proof_sha256=existing.proof_sha256,
            )
        else:
            original_name = Path(uploaded.name or "proof.bin").name
            safe_name = default_storage.get_valid_name(original_name) or "proof.bin"
            # Keep proof keys content-addressed for the same reason as task
            # assets: conflicting bytes must never share a COS object key.
            requested_name = (
                f"publication-proofs/{publication.pk}/{root.hex}/{proof_sha256}/{safe_name}"
            )
            stored_write = None
            try:
                stored_name = default_storage.save(requested_name, uploaded)
                stored_write = StoredObjectWrite(
                    storage=default_storage,
                    stored_name=stored_name,
                    is_referenced=lambda name: PublicationEvent.objects.filter(
                        proof_reference=name
                    ).exists(),
                )
                with transaction.atomic(durable=True):
                    record_manual_publication_proof(
                        publication=publication,
                        publisher_principal=request.user,
                        command_id=root,
                        external_url=form.cleaned_data["external_url"],
                        external_publication_id=form.cleaned_data["external_publication_id"],
                        proof_reference=stored_name,
                        proof_sha256=proof_sha256,
                    )
                    stored_write.retain_on_commit()
            except Exception:
                if stored_write is not None:
                    stored_write.cleanup_after_rollback()
                raise
        messages.success(request, "人工发布证明已保存；系统没有执行任何外部发布动作。")
    except ValidationError as error:
        messages.error(request, _validation_text(error))
    return redirect("dashboard:release-detail", task_id=task.pk)


@login_required
@require_POST
def release_done_action(request: HttpRequest, task_id) -> HttpResponse:
    task = get_object_or_404(Task.objects.select_related("product"), pk=task_id)
    complete_grant = _require_product_grant(
        request.user, task, PermissionGrant.Action.COMPLETE_TASK
    )
    form = CompleteTaskForm(request.POST, state_version=task.state_version)
    if not form.is_valid():
        return render(
            request,
            "dashboard/release_detail.html",
            _release_context(task, request.user, done_form=form),
            status=400,
        )
    try:
        Task.transition(
            task_id=task.pk,
            to_state=Task.State.DONE,
            command_id=form.cleaned_data["command_id"],
            expected_state_version=form.cleaned_data["expected_state_version"],
            actor_principal=request.user,
            acting_role=request.user.role,
            permission_grant=complete_grant,
            recorded_by_principal=request.user,
            reason="Owner/Admin confirmed immutable manual publication proof.",
        )
        messages.success(request, "发布证明已核对，任务已完成。")
        return redirect("dashboard:release-queue")
    except ValidationError as error:
        messages.error(request, _validation_text(error))
    return redirect("dashboard:release-detail", task_id=task.pk)
