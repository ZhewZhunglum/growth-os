from __future__ import annotations

import hashlib
import json
import re
import uuid
from collections.abc import Iterable
from typing import Any
from urllib.parse import urlsplit

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models, transaction
from django.db.models import Q
from django.utils import timezone

from core.models import UUIDv7Model
from workflow.services import guard_review, guard_submission


SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")


def canonical_sha256(payload: Any) -> str:
    """Return the SHA-256 of the canonical UTF-8 JSON representation."""

    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def validate_sha256(value: str) -> None:
    if not SHA256_PATTERN.fullmatch(value):
        raise ValidationError("Expected a lowercase 64-character SHA-256 digest.")


class ActingRole(models.TextChoices):
    OWNER = "OWNER", "Owner"
    OPERATIONS_ADMIN = "OPERATIONS_ADMIN", "Operations admin"
    OPERATOR = "OPERATOR", "Operator"


class AppendOnlyQuerySet(models.QuerySet):
    def update(self, **kwargs):
        raise ValidationError("Append-only content-operation facts cannot be updated.")

    def delete(self):
        raise ValidationError("Append-only content-operation facts cannot be deleted.")

    def bulk_update(self, objs, fields, batch_size=None):
        raise ValidationError("Append-only content-operation facts cannot be bulk-updated.")

    def bulk_create(
        self,
        objs,
        batch_size=None,
        ignore_conflicts=False,
        update_conflicts=False,
        update_fields=None,
        unique_fields=None,
    ):
        raise ValidationError("Bulk creation bypasses fact validation; create each fact explicitly.")


class AppendOnlyManager(models.Manager.from_queryset(AppendOnlyQuerySet)):
    pass


class AppendOnlyFact(UUIDv7Model):
    """Application-level guard for facts that may only ever be inserted."""

    objects = AppendOnlyManager()

    class Meta:
        abstract = True

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise ValidationError(f"{self.__class__.__name__} is immutable; create a new fact instead.")
        self.full_clean()
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError(f"{self.__class__.__name__} is append-only and cannot be deleted.")


def _validate_grant(
    *, grant, principal, acting_role: str, allowed_actions: set[str], product_id: uuid.UUID | None
) -> None:
    from accounts.authorization import resolve_authorization

    if grant.action not in allowed_actions:
        raise ValidationError({"permission_grant": "The grant does not authorize this action."})
    decision = resolve_authorization(
        principal=principal,
        acting_role=acting_role,
        action=grant.action,
        scope_kind="PRODUCT",
        product=product_id,
    )
    if not decision.allowed or decision.grant is None:
        raise ValidationError({"permission_grant": f"Authorization denied: {decision.reason}."})
    if decision.grant.pk != grant.pk:
        raise ValidationError({"permission_grant": "The command must record the centrally resolved grant."})


class ContentAsset(AppendOnlyFact):
    class AssetKind(models.TextChoices):
        VIDEO = "VIDEO", "Video"
        IMAGE = "IMAGE", "Image"
        COPY = "COPY", "Copy"
        DOCUMENT = "DOCUMENT", "Document"
        OTHER = "OTHER", "Other"

    task = models.ForeignKey("workflow.Task", on_delete=models.PROTECT, related_name="content_assets")
    asset_key = models.CharField(max_length=100)
    title = models.CharField(max_length=240)
    asset_kind = models.CharField(max_length=16, choices=AssetKind.choices)
    description = models.TextField(blank=True)
    creation_command_id = models.UUIDField(unique=True)
    creation_payload_hash = models.CharField(max_length=64, validators=[validate_sha256])
    created_by_principal = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="content_assets_created"
    )
    created_by_acting_role = models.CharField(max_length=24, choices=ActingRole.choices)
    created_under_grant = models.ForeignKey(
        "accounts.PermissionGrant", on_delete=models.PROTECT, related_name="content_assets_created"
    )
    recorded_by_principal = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="content_assets_recorded"
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["task", "asset_key"], name="contentops_unique_asset_key_per_task"),
            models.CheckConstraint(condition=~Q(asset_key=""), name="contentops_asset_key_not_empty"),
        ]

    def command_payload(self) -> dict[str, Any]:
        return {
            "task_id": str(self.task_id),
            "asset_key": self.asset_key,
            "title": self.title,
            "asset_kind": self.asset_kind,
            "description": self.description,
        }

    def clean(self):
        super().clean()
        expected_hash = canonical_sha256(self.command_payload())
        if not self.creation_payload_hash:
            self.creation_payload_hash = expected_hash
        elif self.creation_payload_hash != expected_hash:
            raise ValidationError({"creation_payload_hash": "Payload hash does not match the canonical command."})
        if self.created_under_grant_id and self.created_by_principal_id and self.task_id:
            _validate_grant(
                grant=self.created_under_grant,
                principal=self.created_by_principal,
                acting_role=self.created_by_acting_role,
                allowed_actions={"EDIT"},
                product_id=self.task.product_id,
            )

    @classmethod
    def create_idempotent(
        cls,
        *,
        task,
        asset_key: str,
        title: str,
        asset_kind: str,
        description: str = "",
        command_id: uuid.UUID,
        actor_principal,
        acting_role: str,
        permission_grant,
        recorded_by_principal,
    ) -> ContentAsset:
        payload_hash = canonical_sha256(
            {
                "task_id": str(task.pk),
                "asset_key": asset_key,
                "title": title,
                "asset_kind": asset_kind,
                "description": description,
            }
        )
        existing = cls.objects.filter(creation_command_id=command_id).first()
        if existing:
            if existing.creation_payload_hash != payload_hash:
                raise ValidationError("The command_id was already used with a different payload.")
            return existing
        return cls.objects.create(
            task=task,
            asset_key=asset_key,
            title=title,
            asset_kind=asset_kind,
            description=description,
            creation_command_id=command_id,
            creation_payload_hash=payload_hash,
            created_by_principal=actor_principal,
            created_by_acting_role=acting_role,
            created_under_grant=permission_grant,
            recorded_by_principal=recorded_by_principal,
        )

    def __str__(self) -> str:
        return f"{self.asset_key}: {self.title}"


class ContentAssetVersion(AppendOnlyFact):
    class PayloadSchemaVersion(models.IntegerChoices):
        V1 = 1, "Legacy object-key payload"
        V2 = 2, "Representation-aware payload"

    class RepresentationKind(models.TextChoices):
        EXTERNAL_URL = "EXTERNAL_URL", "External URL"
        INLINE_TEXT = "INLINE_TEXT", "Inline text"

    content_asset = models.ForeignKey(ContentAsset, on_delete=models.PROTECT, related_name="versions")
    version_number = models.PositiveIntegerField()
    payload_schema_version = models.PositiveSmallIntegerField(
        choices=PayloadSchemaVersion.choices,
        default=PayloadSchemaVersion.V2,
    )
    representation_kind = models.CharField(
        max_length=16,
        choices=RepresentationKind.choices,
        default=RepresentationKind.EXTERNAL_URL,
    )
    object_key = models.CharField(max_length=1024, blank=True, default="")
    inline_content = models.TextField(blank=True, default="")
    mime_type = models.CharField(max_length=255)
    byte_size = models.PositiveBigIntegerField()
    content_sha256 = models.CharField(max_length=64, validators=[validate_sha256])
    metadata = models.JSONField(default=dict, blank=True)
    manifest_sha256 = models.CharField(max_length=64, validators=[validate_sha256], blank=True)
    creation_command_id = models.UUIDField(unique=True)
    creation_payload_hash = models.CharField(max_length=64, validators=[validate_sha256], blank=True)
    created_by_principal = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="content_asset_versions_created"
    )
    created_by_acting_role = models.CharField(max_length=24, choices=ActingRole.choices)
    created_under_grant = models.ForeignKey(
        "accounts.PermissionGrant", on_delete=models.PROTECT, related_name="content_asset_versions_created"
    )
    recorded_by_principal = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="content_asset_versions_recorded"
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["content_asset_id", "version_number"]
        constraints = [
            models.UniqueConstraint(
                fields=["content_asset", "version_number"], name="contentops_unique_asset_version"
            ),
            models.CheckConstraint(condition=Q(version_number__gte=1), name="contentops_asset_version_gte_one"),
            models.CheckConstraint(
                condition=(
                    Q(representation_kind="EXTERNAL_URL")
                    & ~Q(object_key="")
                    & Q(inline_content="")
                )
                | (
                    Q(representation_kind="INLINE_TEXT")
                    & Q(object_key="")
                    & ~Q(inline_content="")
                ),
                name="contentops_asset_version_representation_valid",
            ),
        ]

    def command_payload(self, *, schema_version: int | None = None) -> dict[str, Any]:
        version = self.payload_schema_version if schema_version is None else schema_version
        legacy_payload = {
            "content_asset_id": str(self.content_asset_id),
            "object_key": self.object_key,
            "mime_type": self.mime_type,
            "byte_size": self.byte_size,
            "content_sha256": self.content_sha256,
            "metadata": self.metadata,
        }
        if version == self.PayloadSchemaVersion.V1:
            return legacy_payload
        if version != self.PayloadSchemaVersion.V2:
            raise ValidationError({"payload_schema_version": "Unsupported content payload schema version."})
        return {
            **legacy_payload,
            "payload_schema_version": self.PayloadSchemaVersion.V2,
            "representation_kind": self.representation_kind,
            "inline_content": self.inline_content,
        }

    def manifest_payload(self) -> dict[str, Any]:
        return {**self.command_payload(), "version_number": self.version_number}

    def clean(self):
        super().clean()
        if self.payload_schema_version == self.PayloadSchemaVersion.V1:
            if self.representation_kind != self.RepresentationKind.EXTERNAL_URL or self.inline_content:
                raise ValidationError(
                    {"representation_kind": "Legacy content payloads must remain object-key references."}
                )
        elif self.payload_schema_version != self.PayloadSchemaVersion.V2:
            raise ValidationError({"payload_schema_version": "Unsupported content payload schema version."})
        elif self.representation_kind == self.RepresentationKind.EXTERNAL_URL:
            parsed = urlsplit(self.object_key)
            if parsed.scheme not in {"http", "https"} or not parsed.netloc:
                raise ValidationError({"object_key": "External content must use an absolute HTTP(S) URL."})
            if parsed.username or parsed.password:
                raise ValidationError({"object_key": "External content URLs must not embed credentials."})
            if self.inline_content:
                raise ValidationError({"inline_content": "External URL content cannot also contain inline text."})
        elif self.representation_kind == self.RepresentationKind.INLINE_TEXT:
            if self.object_key:
                raise ValidationError({"object_key": "Inline text content cannot also contain an external URL."})
            if not self.inline_content.strip():
                raise ValidationError({"inline_content": "Inline text content cannot be blank."})
            inline_bytes = self.inline_content.encode("utf-8")
            if self.byte_size != len(inline_bytes):
                raise ValidationError({"byte_size": "Inline byte size must match the UTF-8 content."})
            if self.content_sha256 != hashlib.sha256(inline_bytes).hexdigest():
                raise ValidationError({"content_sha256": "Inline content hash does not match the UTF-8 content."})
        else:
            raise ValidationError({"representation_kind": "Unsupported content representation."})

        expected_payload_hash = canonical_sha256(self.command_payload())
        expected_manifest_hash = canonical_sha256(self.manifest_payload())
        if not self.creation_payload_hash:
            self.creation_payload_hash = expected_payload_hash
        elif self.creation_payload_hash != expected_payload_hash:
            raise ValidationError({"creation_payload_hash": "Payload hash does not match the canonical command."})
        if not self.manifest_sha256:
            self.manifest_sha256 = expected_manifest_hash
        elif self.manifest_sha256 != expected_manifest_hash:
            raise ValidationError({"manifest_sha256": "Manifest hash does not match the immutable version fields."})
        if self.created_under_grant_id and self.created_by_principal_id and self.content_asset_id:
            _validate_grant(
                grant=self.created_under_grant,
                principal=self.created_by_principal,
                acting_role=self.created_by_acting_role,
                allowed_actions={"EDIT"},
                product_id=self.content_asset.task.product_id,
            )

    @classmethod
    def create_next(
        cls,
        *,
        content_asset: ContentAsset,
        object_key: str = "",
        inline_content: str = "",
        representation_kind: str = RepresentationKind.EXTERNAL_URL,
        mime_type: str,
        byte_size: int | None = None,
        content_sha256: str | None = None,
        metadata: dict[str, Any] | None = None,
        command_id: uuid.UUID,
        actor_principal,
        acting_role: str,
        permission_grant,
        recorded_by_principal,
    ) -> ContentAssetVersion:
        metadata = metadata or {}
        if representation_kind == cls.RepresentationKind.INLINE_TEXT:
            inline_bytes = inline_content.encode("utf-8")
            if byte_size is None:
                byte_size = len(inline_bytes)
            if content_sha256 is None:
                content_sha256 = hashlib.sha256(inline_bytes).hexdigest()
        elif byte_size is None or content_sha256 is None:
            raise ValidationError("External URL versions require byte_size and content_sha256.")

        provisional = cls(
            content_asset=content_asset,
            version_number=1,
            payload_schema_version=cls.PayloadSchemaVersion.V2,
            representation_kind=representation_kind,
            object_key=object_key,
            inline_content=inline_content,
            mime_type=mime_type,
            byte_size=byte_size,
            content_sha256=content_sha256,
            metadata=metadata,
        )
        payload_hash = canonical_sha256(provisional.command_payload())
        existing = cls.objects.filter(creation_command_id=command_id).first()
        if existing:
            replay_hash = canonical_sha256(
                provisional.command_payload(schema_version=existing.payload_schema_version)
            )
            if existing.creation_payload_hash != replay_hash:
                raise ValidationError("The command_id was already used with a different payload.")
            return existing

        with transaction.atomic():
            locked_asset = ContentAsset.objects.select_for_update().get(pk=content_asset.pk)
            latest = cls.objects.filter(content_asset=locked_asset).order_by("-version_number").first()
            next_number = 1 if latest is None else latest.version_number + 1
            return cls.objects.create(
                content_asset=locked_asset,
                version_number=next_number,
                payload_schema_version=cls.PayloadSchemaVersion.V2,
                representation_kind=representation_kind,
                object_key=object_key,
                inline_content=inline_content,
                mime_type=mime_type,
                byte_size=byte_size,
                content_sha256=content_sha256,
                metadata=metadata,
                creation_command_id=command_id,
                creation_payload_hash=payload_hash,
                created_by_principal=actor_principal,
                created_by_acting_role=acting_role,
                created_under_grant=permission_grant,
                recorded_by_principal=recorded_by_principal,
            )

    def __str__(self) -> str:
        return f"{self.content_asset.asset_key} v{self.version_number}"


class TaskSubmission(AppendOnlyFact):
    task = models.ForeignKey("workflow.Task", on_delete=models.PROTECT, related_name="submissions")
    dod_check_run = models.OneToOneField(
        "workflow.TaskCheckRun", on_delete=models.PROTECT, related_name="sealed_submission"
    )
    submission_number = models.PositiveIntegerField()
    primary_asset_version = models.ForeignKey(
        ContentAssetVersion, on_delete=models.PROTECT, related_name="primary_for_submissions"
    )
    supersedes_submission = models.OneToOneField(
        "self",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="superseded_by_submission",
    )
    triggering_review = models.OneToOneField(
        "contentops.ReviewDecision",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="rework_submission",
    )
    asset_manifest = models.JSONField()
    submission_note = models.TextField(blank=True)
    manifest_sha256 = models.CharField(max_length=64, validators=[validate_sha256], blank=True)
    command_id = models.UUIDField(unique=True)
    payload_hash = models.CharField(max_length=64, validators=[validate_sha256], blank=True)
    expected_task_version = models.PositiveIntegerField()
    submitted_by_principal = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="task_submissions_made"
    )
    submitted_by_acting_role = models.CharField(max_length=24, choices=ActingRole.choices)
    submitted_under_grant = models.ForeignKey(
        "accounts.PermissionGrant", on_delete=models.PROTECT, related_name="task_submissions_made"
    )
    recorded_by_principal = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="task_submissions_recorded"
    )
    sealed_at = models.DateTimeField()

    class Meta:
        ordering = ["task_id", "submission_number"]
        constraints = [
            models.UniqueConstraint(
                fields=["task", "submission_number"], name="contentops_unique_submission_number"
            ),
            models.CheckConstraint(condition=Q(submission_number__gte=1), name="contentops_submission_number_gte_one"),
            models.CheckConstraint(
                condition=(
                    Q(submission_number=1, supersedes_submission__isnull=True, triggering_review__isnull=True)
                    | Q(submission_number__gt=1, supersedes_submission__isnull=False)
                ),
                name="contentops_submission_rework_reference",
            ),
            models.CheckConstraint(
                condition=~Q(id=models.F("supersedes_submission_id")),
                name="contentops_submission_cannot_supersede_self",
            ),
        ]

    def _normalized_asset_manifest(self) -> list[dict[str, str]]:
        if not isinstance(self.asset_manifest, list):
            raise ValidationError({"asset_manifest": "Asset manifest must be a list."})
        normalized: list[dict[str, str]] = []
        seen: set[str] = set()
        primary_count = 0
        for item in self.asset_manifest:
            if not isinstance(item, dict) or set(item) != {"asset_version_id", "role"}:
                raise ValidationError(
                    {"asset_manifest": "Each manifest entry must contain only asset_version_id and role."}
                )
            asset_version_id = str(item["asset_version_id"])
            role = str(item["role"])
            try:
                uuid.UUID(asset_version_id)
            except (TypeError, ValueError):
                raise ValidationError({"asset_manifest": "Every asset_version_id must be a UUID."}) from None
            if asset_version_id in seen:
                raise ValidationError({"asset_manifest": "An asset version may appear only once."})
            if role not in {"PRIMARY", "SUPPORTING"}:
                raise ValidationError({"asset_manifest": "Asset role must be PRIMARY or SUPPORTING."})
            if role == "PRIMARY":
                primary_count += 1
            seen.add(asset_version_id)
            normalized.append({"asset_version_id": asset_version_id, "role": role})
        if primary_count != 1:
            raise ValidationError({"asset_manifest": "A sealed submission requires exactly one primary deliverable."})
        expected_primary = str(self.primary_asset_version_id)
        if not any(
            item["role"] == "PRIMARY" and item["asset_version_id"] == expected_primary for item in normalized
        ):
            raise ValidationError({"asset_manifest": "The exact primary entry must match primary_asset_version."})
        return sorted(normalized, key=lambda item: (item["role"] != "PRIMARY", item["asset_version_id"]))

    def command_payload(self) -> dict[str, Any]:
        return {
            "task_id": str(self.task_id),
            "dod_check_run_id": str(self.dod_check_run_id),
            "primary_asset_version_id": str(self.primary_asset_version_id),
            "asset_manifest": self._normalized_asset_manifest(),
            "submission_note": self.submission_note,
            "supersedes_submission_id": str(self.supersedes_submission_id) if self.supersedes_submission_id else None,
            "triggering_review_id": str(self.triggering_review_id) if self.triggering_review_id else None,
            "expected_task_version": self.expected_task_version,
        }

    def manifest_payload(self) -> dict[str, Any]:
        return {**self.command_payload(), "submission_number": self.submission_number}

    def _validate_passing_dod(self) -> None:
        run = self.dod_check_run
        errors: list[str] = []
        if run.task_id != self.task_id:
            errors.append("The DoD run belongs to a different task.")
        if run.check_kind != "DOD":
            errors.append("Submission requires a DoD check run.")
        if run.status != "COMPLETED" or run.aggregate_result != "PASS":
            errors.append("Submission requires a completed passing DoD run.")
        if run.actual_criterion_count != run.expected_criterion_count:
            errors.append("Submission requires a complete DoD criterion set.")
        if getattr(run, "contract_version_id", None) != self.task.contract_version_id:
            errors.append("The DoD run must bind the task's exact contract version.")
        if errors:
            raise ValidationError({"dod_check_run": errors})

    def _validate_rework(self) -> None:
        if self.submission_number == 1:
            if self.supersedes_submission_id or self.triggering_review_id:
                raise ValidationError("The first submission cannot be a rework submission.")
            return
        if not self.supersedes_submission_id:
            raise ValidationError({"supersedes_submission": "Rework must identify the superseded submission."})
        if self.pk and self.supersedes_submission_id == self.pk:
            raise ValidationError({"supersedes_submission": "A submission cannot supersede itself."})
        previous = self.supersedes_submission
        if previous.task_id != self.task_id:
            raise ValidationError({"supersedes_submission": "The superseded submission belongs to another task."})
        if self.submission_number != previous.submission_number + 1:
            raise ValidationError({"submission_number": "Rework must be the next submission in sequence."})
        latest = type(self).objects.filter(task_id=self.task_id).order_by("-submission_number").first()
        if self._state.adding and latest is not None and latest.pk != previous.pk:
            raise ValidationError({"supersedes_submission": "Rework must extend the current submission chain tip."})
        if self.dod_check_run_id == previous.dod_check_run_id:
            raise ValidationError({"dod_check_run": "Rework requires a new DoD check run."})
        old_primary = previous.primary_asset_version
        new_primary = self.primary_asset_version
        if new_primary.content_asset_id != old_primary.content_asset_id:
            raise ValidationError(
                {"primary_asset_version": "Rework must create a new version of the same primary asset."}
            )
        if new_primary.version_number <= old_primary.version_number:
            raise ValidationError({"primary_asset_version": "Rework requires a newer primary asset version."})
        try:
            final_review = previous.final_review
        except ReviewDecision.DoesNotExist:
            final_review = None
        withdrawal = previous.withdrawal_events.filter(event_type="SUBMISSION_WITHDRAWN").first()
        if final_review is not None:
            if final_review.decision != ReviewDecision.Decision.CHANGES_REQUESTED:
                raise ValidationError({"supersedes_submission": "Only CHANGES_REQUESTED may trigger human rework."})
            if self.triggering_review_id != final_review.pk:
                raise ValidationError({"triggering_review": "Human rework must bind the exact final review."})
            if withdrawal is not None:
                raise ValidationError("A submission cannot be both reviewed and withdrawn.")
        elif withdrawal is not None:
            if withdrawal.task_id != self.task_id or withdrawal.submission_id != previous.pk:
                raise ValidationError({"supersedes_submission": "Withdrawal fact does not match this task chain."})
            if self.triggering_review_id is not None:
                raise ValidationError({"triggering_review": "Withdrawal rework cannot bind a review decision."})
        else:
            raise ValidationError(
                {"supersedes_submission": "Rework requires exact CHANGES_REQUESTED or withdrawal evidence."}
            )

    def clean(self):
        super().clean()
        if self.task_id and self.expected_task_version != self.task.state_version:
            raise ValidationError({"expected_task_version": "Task version is stale; reread before submitting."})
        if self.primary_asset_version_id and self.task_id:
            if self.primary_asset_version.content_asset.task_id != self.task_id:
                raise ValidationError({"primary_asset_version": "The primary asset belongs to a different task."})
        if self.dod_check_run_id and self.task_id:
            self._validate_passing_dod()
        normalized = self._normalized_asset_manifest()
        manifest_ids = [uuid.UUID(item["asset_version_id"]) for item in normalized]
        if self.task_id:
            actual_versions = ContentAssetVersion.objects.filter(pk__in=manifest_ids).select_related("content_asset")
            actual_by_id = {str(version.pk): version for version in actual_versions}
            if len(actual_by_id) != len(manifest_ids):
                raise ValidationError({"asset_manifest": "Every exact asset version must exist."})
            if any(version.content_asset.task_id != self.task_id for version in actual_by_id.values()):
                raise ValidationError({"asset_manifest": "Every asset version must belong to the submission task."})
        if self.submission_number:
            self._validate_rework()
        expected_payload_hash = canonical_sha256(self.command_payload())
        expected_manifest_hash = canonical_sha256(self.manifest_payload())
        if not self.payload_hash:
            self.payload_hash = expected_payload_hash
        elif self.payload_hash != expected_payload_hash:
            raise ValidationError({"payload_hash": "Payload hash does not match the canonical submission command."})
        if not self.manifest_sha256:
            self.manifest_sha256 = expected_manifest_hash
        elif self.manifest_sha256 != expected_manifest_hash:
            raise ValidationError({"manifest_sha256": "Manifest hash does not match the sealed submission."})
        if self.submitted_under_grant_id and self.submitted_by_principal_id and self.task_id:
            _validate_grant(
                grant=self.submitted_under_grant,
                principal=self.submitted_by_principal,
                acting_role=self.submitted_by_acting_role,
                allowed_actions={"EDIT"},
                product_id=self.task.product_id,
            )

    @classmethod
    def seal(
        cls,
        *,
        task,
        dod_check_run,
        primary_asset_version: ContentAssetVersion,
        supporting_asset_versions: Iterable[ContentAssetVersion] = (),
        supersedes_submission: TaskSubmission | None = None,
        triggering_review: ReviewDecision | None = None,
        submission_note: str = "",
        command_id: uuid.UUID,
        expected_task_version: int,
        actor_principal,
        acting_role: str,
        permission_grant,
        recorded_by_principal,
    ) -> TaskSubmission:
        if triggering_review is not None:
            if supersedes_submission is None:
                supersedes_submission = triggering_review.submission
            elif triggering_review.submission_id != supersedes_submission.pk:
                raise ValidationError("Triggering review and superseded submission do not match.")
        supporting = list(supporting_asset_versions)
        asset_versions = [primary_asset_version, *supporting]
        asset_ids = [version.pk for version in asset_versions]
        if len(set(asset_ids)) != len(asset_ids):
            raise ValidationError("An asset version may appear only once in a submission.")
        asset_manifest = [
            {"asset_version_id": str(primary_asset_version.pk), "role": "PRIMARY"},
            *[
                {"asset_version_id": str(version.pk), "role": "SUPPORTING"}
                for version in sorted(supporting, key=lambda version: str(version.pk))
            ],
        ]
        provisional = cls(
            task=task,
            dod_check_run=dod_check_run,
            submission_number=(supersedes_submission.submission_number + 1 if supersedes_submission else 1),
            primary_asset_version=primary_asset_version,
            supersedes_submission=supersedes_submission,
            triggering_review=triggering_review,
            asset_manifest=asset_manifest,
            submission_note=submission_note,
            command_id=command_id,
            expected_task_version=expected_task_version,
            submitted_by_principal=actor_principal,
            submitted_by_acting_role=acting_role,
            submitted_under_grant=permission_grant,
            recorded_by_principal=recorded_by_principal,
            sealed_at=timezone.now(),
        )
        provisional.payload_hash = canonical_sha256(provisional.command_payload())
        existing = cls.objects.filter(command_id=command_id).first()
        if existing:
            if existing.payload_hash != provisional.payload_hash:
                raise ValidationError("The command_id was already used with a different payload.")
            return existing

        with transaction.atomic():
            locked_task = type(task).objects.select_for_update().get(pk=task.pk)
            if locked_task.current_assignee_principal_id != actor_principal.pk:
                raise ValidationError("Only the task's current assignee may submit deliverables.")
            if supersedes_submission:
                supersedes_submission = cls.objects.select_for_update().get(pk=supersedes_submission.pk)
            submission = provisional
            # Validate the optimistic version against the row protected by the
            # same transaction, not against a potentially stale caller object.
            submission.task = locked_task
            submission.supersedes_submission = supersedes_submission
            guard_submission(locked_task, dod_check_run=dod_check_run)
            submission.save()
            for version in asset_versions:
                TaskSubmissionAssetLink.objects.create(
                    submission=submission,
                    asset_version=version,
                    role=(
                        TaskSubmissionAssetLink.Role.PRIMARY
                        if version.pk == primary_asset_version.pk
                        else TaskSubmissionAssetLink.Role.SUPPORTING
                    ),
                )
            return submission

    def __str__(self) -> str:
        return f"Task {self.task_id} submission #{self.submission_number}"


class TaskSubmissionAssetLink(AppendOnlyFact):
    class Role(models.TextChoices):
        PRIMARY = "PRIMARY", "Primary deliverable"
        SUPPORTING = "SUPPORTING", "Supporting deliverable"

    submission = models.ForeignKey(TaskSubmission, on_delete=models.PROTECT, related_name="asset_links")
    asset_version = models.ForeignKey(
        ContentAssetVersion, on_delete=models.PROTECT, related_name="submission_links"
    )
    role = models.CharField(max_length=16, choices=Role.choices)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["submission", "asset_version"], name="contentops_unique_submission_asset"
            ),
            models.UniqueConstraint(
                fields=["submission"],
                condition=Q(role="PRIMARY"),
                name="contentops_one_primary_link_per_submission",
            ),
        ]

    def clean(self):
        super().clean()
        if not self.submission_id or not self.asset_version_id:
            return
        expected_entry = {
            "asset_version_id": str(self.asset_version_id),
            "role": self.role,
        }
        if expected_entry not in self.submission._normalized_asset_manifest():
            raise ValidationError("The link must exactly match an entry in the sealed asset manifest.")
        if self.asset_version.content_asset.task_id != self.submission.task_id:
            raise ValidationError("The linked asset version belongs to a different task.")
        is_exact_primary = self.asset_version_id == self.submission.primary_asset_version_id
        if (self.role == self.Role.PRIMARY) != is_exact_primary:
            raise ValidationError("The PRIMARY link must be the submission's exact primary asset version.")

    def __str__(self) -> str:
        return f"{self.submission_id} -> {self.asset_version_id} ({self.role})"


class ReviewDecision(AppendOnlyFact):
    class PayloadSchemaVersion(models.IntegerChoices):
        V1 = 1, "Legacy review command"
        V2 = 2, "Reviewer-bound review command"

    class Decision(models.TextChoices):
        APPROVED = "APPROVED", "Approved"
        CHANGES_REQUESTED = "CHANGES_REQUESTED", "Changes requested"
        REJECTED = "REJECTED", "Rejected"

    submission = models.OneToOneField(TaskSubmission, on_delete=models.PROTECT, related_name="final_review")
    decision = models.CharField(max_length=24, choices=Decision.choices)
    rationale = models.TextField()
    criteria_results = models.JSONField(default=dict, blank=True)
    decision_sha256 = models.CharField(max_length=64, validators=[validate_sha256], blank=True)
    command_id = models.UUIDField(unique=True)
    payload_hash = models.CharField(max_length=64, validators=[validate_sha256], blank=True)
    payload_schema_version = models.PositiveSmallIntegerField(
        choices=PayloadSchemaVersion.choices,
        default=PayloadSchemaVersion.V2,
    )
    expected_task_version = models.PositiveIntegerField()
    reviewer_principal = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="review_decisions_made"
    )
    reviewer_acting_role = models.CharField(max_length=24, choices=ActingRole.choices)
    reviewer_grant = models.ForeignKey(
        "accounts.PermissionGrant", on_delete=models.PROTECT, related_name="review_decisions_made"
    )
    recorded_by_principal = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="review_decisions_recorded"
    )
    decided_at = models.DateTimeField()

    class Meta:
        ordering = ["decided_at", "id"]

    def command_payload(self, *, schema_version: int | None = None) -> dict[str, Any]:
        """Return the exact payload shape used by the stored hash.

        V1 decisions predate reviewer binding in the command hash.  Their
        stored hashes are historical facts and must continue to validate and
        replay without being rewritten.  All new decisions are V2.
        """

        version = self.payload_schema_version if schema_version is None else schema_version
        legacy_payload = {
            "submission_id": str(self.submission_id),
            "decision": self.decision,
            "rationale": self.rationale,
            "criteria_results": self.criteria_results,
            "expected_task_version": self.expected_task_version,
        }
        if version == self.PayloadSchemaVersion.V1:
            return legacy_payload
        if version != self.PayloadSchemaVersion.V2:
            raise ValidationError({"payload_schema_version": "Unsupported review payload schema version."})
        return {
            **legacy_payload,
            "payload_schema_version": self.PayloadSchemaVersion.V2,
            "reviewer_principal_id": str(self.reviewer_principal_id),
            "reviewer_acting_role": self.reviewer_acting_role,
            "reviewer_grant_id": str(self.reviewer_grant_id),
        }

    def clean(self):
        super().clean()
        expected_hash = canonical_sha256(self.command_payload())
        if not self.payload_hash:
            self.payload_hash = expected_hash
        elif self.payload_hash != expected_hash:
            raise ValidationError({"payload_hash": "Payload hash does not match the canonical review command."})
        if not self.decision_sha256:
            self.decision_sha256 = expected_hash
        elif self.decision_sha256 != expected_hash:
            raise ValidationError({"decision_sha256": "Decision hash does not match the immutable review fields."})
        if self.submission_id and self.expected_task_version != self.submission.task.state_version:
            raise ValidationError({"expected_task_version": "Task version is stale; reread before reviewing."})
        if self.reviewer_principal_id and self.reviewer_principal.principal_type != "HUMAN_USER":
            raise ValidationError({"reviewer_principal": "V1 requires a human final reviewer."})
        if (
            self.submission_id
            and self.reviewer_principal_id
            and self.reviewer_principal_id == self.submission.submitted_by_principal_id
        ):
            raise ValidationError({"reviewer_principal": "A submitter cannot review their own submission."})
        if self.reviewer_grant_id and self.reviewer_principal_id and self.submission_id:
            _validate_grant(
                grant=self.reviewer_grant,
                principal=self.reviewer_principal,
                acting_role=self.reviewer_acting_role,
                allowed_actions={"REVIEW", "APPROVE"},
                product_id=self.submission.task.product_id,
            )

    def save(self, *args, **kwargs):
        if not getattr(self, "_record_final_authorized", False):
            raise ValidationError("ReviewDecision must be written through record_final().")
        return super().save(*args, **kwargs)

    @classmethod
    def record_final(
        cls,
        *,
        submission: TaskSubmission,
        decision: str,
        rationale: str,
        criteria_results: dict[str, Any] | None = None,
        command_id: uuid.UUID,
        expected_task_version: int,
        reviewer_principal,
        acting_role: str,
        permission_grant,
        recorded_by_principal,
    ) -> ReviewDecision:
        criteria_results = criteria_results or {}
        provisional = cls(
            submission=submission,
            decision=decision,
            rationale=rationale,
            criteria_results=criteria_results,
            command_id=command_id,
            payload_schema_version=cls.PayloadSchemaVersion.V2,
            expected_task_version=expected_task_version,
            reviewer_principal=reviewer_principal,
            reviewer_acting_role=acting_role,
            reviewer_grant=permission_grant,
            recorded_by_principal=recorded_by_principal,
            decided_at=timezone.now(),
        )
        provisional.payload_hash = canonical_sha256(provisional.command_payload())
        provisional.decision_sha256 = provisional.payload_hash
        if reviewer_principal.pk == submission.submitted_by_principal_id:
            raise ValidationError({"reviewer_principal": "A submitter cannot review their own submission."})

        with transaction.atomic():
            from workflow.models import Task, TaskStateEvent

            # Withdrawal uses this same Task -> Submission order.  The first
            # transaction to acquire the task lock wins; the other must reread.
            locked_task = Task.objects.select_for_update().get(pk=submission.task_id)
            locked_submission = TaskSubmission.objects.select_for_update().get(pk=submission.pk)
            locked_submission.task = locked_task
            provisional.submission = locked_submission

            existing_command = cls.objects.filter(command_id=command_id).first()
            if existing_command:
                replay_hash = canonical_sha256(
                    provisional.command_payload(schema_version=existing_command.payload_schema_version)
                )
                if (
                    existing_command.payload_hash != replay_hash
                    or existing_command.decision_sha256 != existing_command.payload_hash
                    or existing_command.submission_id != locked_submission.pk
                    or existing_command.reviewer_principal_id != reviewer_principal.pk
                    or existing_command.reviewer_acting_role != acting_role
                    or existing_command.reviewer_grant_id != permission_grant.pk
                ):
                    raise ValidationError("The command_id was already used with a different review command.")
                return existing_command

            guard_review(locked_task, submission=locked_submission)
            if locked_submission.submitted_by_principal_id == reviewer_principal.pk:
                raise ValidationError({"reviewer_principal": "A submitter cannot review their own submission."})
            if TaskStateEvent.objects.filter(
                event_type=TaskStateEvent.EventType.SUBMISSION_WITHDRAWN,
                submission=locked_submission,
            ).exists():
                raise ValidationError("A withdrawn submission cannot receive a review decision.")
            if cls.objects.filter(submission=locked_submission).exists():
                raise ValidationError("This submission already has its one final review decision.")
            provisional._record_final_authorized = True
            provisional.save()
            return provisional

    def __str__(self) -> str:
        return f"{self.submission_id}: {self.decision}"
