from __future__ import annotations

import hashlib
import json
import re
import uuid
from datetime import timedelta
from typing import Any, Iterable

from django.conf import settings
from django.core.exceptions import ObjectDoesNotExist, ValidationError
from django.db import models, transaction
from django.db.models import Q
from django.utils import timezone

from core.models import TimeStampedModel, UUIDv7Model
from workflow.services import guard_manual_publication, guard_release_gate


SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


def canonical_sha256(payload: Any) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def validate_sha256(value: str) -> None:
    if not SHA256_RE.fullmatch(value):
        raise ValidationError("Expected a lowercase 64-character SHA-256 digest.")


class ActingRole(models.TextChoices):
    OWNER = "OWNER", "Owner"
    OPERATIONS_ADMIN = "OPERATIONS_ADMIN", "Operations admin"
    OPERATOR = "OPERATOR", "Operator"
    SYSTEM = "SYSTEM", "System"


class ImmutableQuerySet(models.QuerySet):
    def update(self, **kwargs):
        raise ValidationError("Immutable release facts cannot be updated.")

    def delete(self):
        raise ValidationError("Immutable release facts cannot be deleted.")

    def bulk_update(self, objs, fields, batch_size=None):
        raise ValidationError("Immutable release facts cannot be bulk-updated.")

    def bulk_create(
        self,
        objs,
        batch_size=None,
        ignore_conflicts=False,
        update_conflicts=False,
        update_fields=None,
        unique_fields=None,
    ):
        raise ValidationError("Bulk creation bypasses release-fact authorization and validation.")


class ImmutableManager(models.Manager.from_queryset(ImmutableQuerySet)):
    pass


class ImmutableFact(UUIDv7Model):
    objects = ImmutableManager()

    class Meta:
        abstract = True

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise ValidationError(f"{self.__class__.__name__} is immutable; create a new fact.")
        self.full_clean()
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError(f"{self.__class__.__name__} is append-only.")


class PolicyDefinition(TimeStampedModel):
    class Status(models.TextChoices):
        ACTIVE = "ACTIVE", "Active"
        RETIRED = "RETIRED", "Retired"

    policy_code = models.CharField(max_length=100, unique=True)
    name = models.CharField(max_length=240)
    description = models.TextField(blank=True)
    is_mandatory = models.BooleanField(default=True)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.ACTIVE)
    created_by_principal = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="policy_definitions_created"
    )
    updated_by_principal = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="policy_definitions_updated"
    )

    class Meta:
        ordering = ["policy_code"]
        constraints = [models.CheckConstraint(condition=~Q(policy_code=""), name="releasegate_policy_code_not_empty")]

    def __str__(self) -> str:
        return self.policy_code


def _rule_code(rule: dict[str, Any]) -> str:
    return str(rule.get("rule_code", rule.get("code", ""))).strip()


class PolicyVersionManager(ImmutableManager):
    def current_mandatory(self, at=None) -> list[PolicyVersion]:
        at = at or timezone.now()
        candidates = self.select_related("policy_definition").filter(
            policy_definition__status=PolicyDefinition.Status.ACTIVE,
            policy_definition__is_mandatory=True,
            effective_from__lte=at,
        ).filter(Q(effective_until__isnull=True) | Q(effective_until__gt=at))
        current: dict[uuid.UUID, PolicyVersion] = {}
        for version in candidates.order_by("policy_definition_id", "version_number"):
            current[version.policy_definition_id] = version
        return list(current.values())


class PolicyVersion(ImmutableFact):
    policy_definition = models.ForeignKey(PolicyDefinition, on_delete=models.PROTECT, related_name="versions")
    version_number = models.PositiveIntegerField()
    rules = models.JSONField()
    effective_from = models.DateTimeField(default=timezone.now)
    effective_until = models.DateTimeField(null=True, blank=True)
    manifest_sha256 = models.CharField(max_length=64, validators=[validate_sha256], blank=True)
    created_by_principal = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="policy_versions_created"
    )
    recorded_by_principal = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="policy_versions_recorded"
    )
    created_at = models.DateTimeField(auto_now_add=True)

    objects = PolicyVersionManager()

    class Meta:
        ordering = ["policy_definition_id", "version_number"]
        constraints = [
            models.UniqueConstraint(
                fields=["policy_definition", "version_number"], name="releasegate_unique_policy_version"
            ),
            models.CheckConstraint(condition=Q(version_number__gte=1), name="releasegate_policy_version_gte_one"),
            models.CheckConstraint(
                condition=Q(effective_until__isnull=True) | Q(effective_until__gt=models.F("effective_from")),
                name="releasegate_policy_effective_window",
            ),
        ]

    def normalized_rules(self) -> list[dict[str, Any]]:
        if not isinstance(self.rules, list) or not self.rules:
            raise ValidationError({"rules": "A policy version requires a non-empty rule list."})
        normalized: list[dict[str, Any]] = []
        seen: set[str] = set()
        for rule in self.rules:
            if not isinstance(rule, dict) or not _rule_code(rule):
                raise ValidationError({"rules": "Every rule requires a rule_code (or code)."})
            code = _rule_code(rule)
            if code in seen:
                raise ValidationError({"rules": f"Duplicate rule code: {code}."})
            seen.add(code)
            normalized.append({"rule_code": code, "required": bool(rule.get("required", True))})
        return sorted(normalized, key=lambda item: item["rule_code"])

    def manifest_payload(self) -> dict[str, Any]:
        return {
            "policy_definition_id": str(self.policy_definition_id),
            "version_number": self.version_number,
            "rules": self.normalized_rules(),
            "effective_from": self.effective_from.isoformat(),
            "effective_until": self.effective_until.isoformat() if self.effective_until else None,
        }

    def clean(self):
        super().clean()
        expected = canonical_sha256(self.manifest_payload())
        if not self.manifest_sha256:
            self.manifest_sha256 = expected
        elif self.manifest_sha256 != expected:
            raise ValidationError({"manifest_sha256": "Manifest does not match the immutable policy version."})


def policy_set_hash(versions: Iterable[PolicyVersion]) -> str:
    return canonical_sha256(
        sorted(
            ({"id": str(version.pk), "manifest_sha256": version.manifest_sha256} for version in versions),
            key=lambda item: item["id"],
        )
    )


def required_policy_versions_for_contract(task_contract_version, *, at=None) -> tuple[list[PolicyVersion], bool]:
    """Resolve the exact fail-closed policy set for a task contract.

    The release set is the union of the contract's immutable required policy
    links and the policy versions that are currently mandatory.  Returning the
    second value separately lets callers distinguish an invalid contract with
    no required exact policy snapshot from an otherwise empty policy registry.
    """

    at = at or timezone.now()
    contract_versions = list(
        PolicyVersion.objects.filter(
            task_contract_links__task_contract_version_id=task_contract_version.pk,
            task_contract_links__required=True,
        )
    )
    versions_by_id = {version.pk: version for version in contract_versions}
    for version in PolicyVersion.objects.current_mandatory(at):
        versions_by_id[version.pk] = version
    return (
        sorted(versions_by_id.values(), key=lambda version: str(version.pk)),
        bool(contract_versions),
    )


def _rule_evaluator_authorization_blockers(*, principal, grant, task_contract_version, at=None) -> list[str]:
    """Return reasons why a rule evaluator is not trusted and authorized.

    V1 deliberately reuses the existing explicit REVIEW PermissionGrant for
    deterministic rule evaluation.  The writer must still be a non-human
    service identity; a Publisher or Reviewer cannot self-attest a PASS.
    """

    at = at or timezone.now()
    from accounts.authorization import resolve_authorization

    blockers: list[str] = []
    if principal.principal_type not in {"SERVICE_ACCOUNT", "SYSTEM"}:
        blockers.append("RULE_EVALUATOR_NOT_SERVICE_PRINCIPAL")
    if principal.principal_status != "ACTIVE" or not principal.is_active:
        blockers.append("RULE_EVALUATOR_NOT_ACTIVE")
    if grant.principal_id != principal.pk:
        blockers.append("RULE_EVALUATOR_GRANT_PRINCIPAL_MISMATCH")
    if grant.action != "REVIEW" or grant.effect != "ALLOW" or grant.grant_status != "ACTIVE":
        blockers.append("RULE_EVALUATOR_GRANT_NOT_ACTIVE_ALLOW_REVIEW")
    if grant.valid_from > at or (grant.valid_until and grant.valid_until <= at):
        blockers.append("RULE_EVALUATOR_GRANT_EXPIRED_OR_NOT_YET_VALID")
    if grant.scope_kind == "PRODUCT":
        product_id = task_contract_version.product_profile_version.product_id
        if grant.product_id != product_id:
            blockers.append("RULE_EVALUATOR_GRANT_PRODUCT_SCOPE_MISMATCH")
    elif grant.scope_kind != "GLOBAL":
        blockers.append("RULE_EVALUATOR_GRANT_SCOPE_INVALID")
    decision = resolve_authorization(
        principal=principal,
        # SYSTEM is the execution context; the persisted role still identifies
        # the service principal's least-privilege authorization profile.
        acting_role=principal.role,
        action="REVIEW",
        scope_kind="PRODUCT",
        product=task_contract_version.product_profile_version.product_id,
        at=at,
    )
    if not decision.allowed:
        blockers.append(f"RULE_EVALUATOR_AUTHORIZATION_{decision.reason}")
    elif decision.grant is None or decision.grant.pk != grant.pk:
        blockers.append("RULE_EVALUATOR_GRANT_NOT_CENTRALLY_RESOLVED")
    return blockers


class ChannelAccount(TimeStampedModel):
    class Status(models.TextChoices):
        ACTIVE = "ACTIVE", "Active"
        SUSPENDED = "SUSPENDED", "Suspended"
        RETIRED = "RETIRED", "Retired"

    platform_code = models.CharField(max_length=64)
    account_code = models.CharField(max_length=100, unique=True)
    external_account_ref = models.CharField(max_length=255)
    display_name = models.CharField(max_length=240)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.ACTIVE)
    created_by_principal = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="channel_accounts_created"
    )
    updated_by_principal = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="channel_accounts_updated"
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["platform_code", "external_account_ref"], name="releasegate_unique_platform_account"
            )
        ]


class RuntimeEnvironment(TimeStampedModel):
    class EnvironmentType(models.TextChoices):
        STAGING = "STAGING", "Staging"
        PRODUCTION = "PRODUCTION", "Production"

    class Status(models.TextChoices):
        ACTIVE = "ACTIVE", "Active"
        LOCKED = "LOCKED", "Locked"
        RETIRED = "RETIRED", "Retired"

    environment_code = models.CharField(max_length=100, unique=True)
    environment_type = models.CharField(max_length=16, choices=EnvironmentType.choices)
    identity_namespace = models.CharField(max_length=255)
    database_namespace = models.CharField(max_length=255)
    object_storage_namespace = models.CharField(max_length=255)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.ACTIVE)
    created_by_principal = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="runtime_environments_created"
    )
    updated_by_principal = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="runtime_environments_updated"
    )


class AccountEnvironmentBinding(ImmutableFact):
    class Status(models.TextChoices):
        ACTIVE = "ACTIVE", "Active"
        REVOKED = "REVOKED", "Revoked"
        EXPIRED = "EXPIRED", "Expired"

    channel_account = models.ForeignKey(ChannelAccount, on_delete=models.PROTECT, related_name="environment_bindings")
    runtime_environment = models.ForeignKey(
        RuntimeEnvironment, on_delete=models.PROTECT, related_name="account_bindings"
    )
    binding_version = models.PositiveIntegerField()
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.ACTIVE)
    identity_reference = models.CharField(max_length=255)
    valid_from = models.DateTimeField(default=timezone.now)
    valid_until = models.DateTimeField(null=True, blank=True)
    supersedes = models.OneToOneField(
        "self", null=True, blank=True, on_delete=models.PROTECT, related_name="superseded_by"
    )
    created_by_principal = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")
    recorded_by_principal = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["channel_account", "runtime_environment", "binding_version"],
                name="releasegate_unique_binding_version",
            ),
            models.CheckConstraint(condition=Q(binding_version__gte=1), name="releasegate_binding_version_gte_one"),
            models.CheckConstraint(
                condition=Q(valid_until__isnull=True) | Q(valid_until__gt=models.F("valid_from")),
                name="releasegate_binding_valid_window",
            ),
        ]

    def clean(self):
        super().clean()
        if self.supersedes_id and (
            self.supersedes.channel_account_id != self.channel_account_id
            or self.supersedes.runtime_environment_id != self.runtime_environment_id
            or self.supersedes.binding_version >= self.binding_version
        ):
            raise ValidationError({"supersedes": "A binding may supersede only an older binding for the same pair."})

    def is_current_at(self, at=None) -> bool:
        at = at or timezone.now()
        latest = type(self).objects.filter(
            channel_account_id=self.channel_account_id, runtime_environment_id=self.runtime_environment_id
        ).order_by("-binding_version").first()
        return bool(
            latest
            and latest.pk == self.pk
            and self.status == self.Status.ACTIVE
            and self.valid_from <= at
            and (self.valid_until is None or self.valid_until > at)
        )


class CapabilityState(ImmutableFact):
    class State(models.TextChoices):
        OPEN = "OPEN", "Open"
        CLOSED = "CLOSED", "Closed"
        UNKNOWN = "UNKNOWN", "Unknown"

    MANUAL_PUBLISH = "MANUAL_PUBLISH"

    account_environment_binding = models.ForeignKey(
        AccountEnvironmentBinding, on_delete=models.PROTECT, related_name="capability_states"
    )
    capability_code = models.CharField(max_length=64, default=MANUAL_PUBLISH)
    state_version = models.PositiveIntegerField()
    state = models.CharField(max_length=16, choices=State.choices)
    effective_from = models.DateTimeField(default=timezone.now)
    expires_at = models.DateTimeField(null=True, blank=True)
    reason = models.TextField(blank=True)
    evidence_reference = models.CharField(max_length=1024, blank=True)
    evidence_sha256 = models.CharField(max_length=64, validators=[validate_sha256], blank=True)
    supersedes = models.OneToOneField(
        "self", null=True, blank=True, on_delete=models.PROTECT, related_name="superseded_by"
    )
    created_by_principal = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")
    recorded_by_principal = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["account_environment_binding", "capability_code", "state_version"],
                name="releasegate_unique_capability_state_version",
            ),
            models.CheckConstraint(condition=Q(state_version__gte=1), name="releasegate_capability_version_gte_one"),
            models.CheckConstraint(
                condition=Q(expires_at__isnull=True) | Q(expires_at__gt=models.F("effective_from")),
                name="releasegate_capability_valid_window",
            ),
        ]

    def clean(self):
        super().clean()
        if self.supersedes_id and (
            self.supersedes.account_environment_binding_id != self.account_environment_binding_id
            or self.supersedes.capability_code != self.capability_code
            or self.supersedes.state_version >= self.state_version
        ):
            raise ValidationError({"supersedes": "A capability state may supersede only its prior version."})

    def is_current_open_at(self, at=None) -> bool:
        at = at or timezone.now()
        latest = type(self).objects.filter(
            account_environment_binding_id=self.account_environment_binding_id,
            capability_code=self.capability_code,
        ).order_by("-state_version").first()
        return bool(
            latest
            and latest.pk == self.pk
            and self.state == self.State.OPEN
            and self.effective_from <= at
            and (self.expires_at is None or self.expires_at > at)
        )


class PublicationManager(models.Manager):
    def create_intent(
        self, *, submission, command_id, payload_hash, actor_principal, acting_role, permission_grant,
        recorded_by_principal
    ) -> Publication:
        existing = self.filter(creation_command_id=command_id).first()
        if existing:
            if existing.creation_payload_hash != payload_hash:
                raise ValidationError("The command_id was already used with a different payload.")
            return existing
        actor_principal.validate_acting_role(acting_role)
        try:
            review_decision = submission.final_review
        except ObjectDoesNotExist:
            review_decision = None
        current_task = type(submission.task).objects.get(pk=submission.task_id)
        guard_release_gate(
            current_task,
            submission=submission,
            review_decision=review_decision,
        )
        if (
            permission_grant.principal_id != actor_principal.pk
            or permission_grant.action != "PUBLISH"
            or permission_grant.effect != "ALLOW"
            or not permission_grant.is_current
        ):
            raise ValidationError("Publication intent requires the actor's current ALLOW PUBLISH grant.")
        with transaction.atomic():
            publication = self.create(
                submission=submission,
                status=Publication.Status.GATE_PENDING,
                state_version=0,
                creation_command_id=command_id,
                creation_payload_hash=payload_hash,
                requested_by_principal=actor_principal,
                requested_by_acting_role=acting_role,
                requested_under_grant=permission_grant,
                recorded_by_principal=recorded_by_principal,
            )
            PublicationEvent.objects.append(
                publication=publication,
                event_type=PublicationEvent.EventType.GATE_PENDING,
                command_id=command_id,
                payload_hash=payload_hash,
                expected_state_version=0,
                actor_principal=actor_principal,
                acting_role=acting_role,
                permission_grant=permission_grant,
                recorded_by_principal=recorded_by_principal,
            )
            publication.refresh_from_db()
            return publication


class Publication(UUIDv7Model):
    class Status(models.TextChoices):
        GATE_PENDING = "GATE_PENDING", "Gate pending"
        GATE_BLOCKED = "GATE_BLOCKED", "Gate blocked"
        READY_FOR_MANUAL_PUBLISH = "READY_FOR_MANUAL_PUBLISH", "Ready for manual publish"
        MANUAL_PUBLISHED_RECORDED = "MANUAL_PUBLISHED_RECORDED", "Manual publication recorded"

    submission = models.ForeignKey("contentops.TaskSubmission", on_delete=models.PROTECT, related_name="publications")
    status = models.CharField(max_length=32, choices=Status.choices, default=Status.GATE_PENDING)
    state_version = models.PositiveIntegerField(default=0)
    current_gate = models.ForeignKey(
        "ReleaseGateRecord", null=True, blank=True, on_delete=models.PROTECT, related_name="current_for_publications"
    )
    creation_command_id = models.UUIDField(unique=True)
    creation_payload_hash = models.CharField(max_length=64, validators=[validate_sha256])
    requested_by_principal = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")
    requested_by_acting_role = models.CharField(max_length=24, choices=ActingRole.choices)
    requested_under_grant = models.ForeignKey("accounts.PermissionGrant", on_delete=models.PROTECT, related_name="+")
    recorded_by_principal = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)

    objects = PublicationManager()

    def clean(self):
        super().clean()
        if self._state.adding and self.status != self.Status.GATE_PENDING:
            raise ValidationError({"status": "A Publication must begin in GATE_PENDING."})
        if self._state.adding and (self.state_version != 0 or self.current_gate_id):
            raise ValidationError("A Publication intent must start at version 0 without a Gate.")

    def save(self, *args, **kwargs):
        if not self._state.adding:
            original = type(self).objects.get(pk=self.pk)
            changed = {
                field.attname
                for field in self._meta.concrete_fields
                if field.attname not in {"id"}
                and getattr(original, field.attname) != getattr(self, field.attname)
            }
            if changed:
                raise ValidationError("Publication projections may change only by appending a PublicationEvent.")
        self.full_clean()
        return super().save(*args, **kwargs)

    def append_event(self, **kwargs):
        return PublicationEvent.objects.append(publication=self, **kwargs)


class RuleEvaluationRunManager(models.Manager):
    def start(
        self, *, publication, policy_versions, context_hash, evaluator_key, command_id, payload_hash,
        initiated_by_principal, acting_role, permission_grant, recorded_by_principal
    ) -> RuleEvaluationRun:
        existing = self.filter(command_id=command_id).first()
        if existing:
            if existing.payload_hash != payload_hash:
                raise ValidationError("The command_id was already used with a different payload.")
            return existing
        versions = list(policy_versions)
        task_contract_version = publication.submission.task.contract_version
        expected_versions, has_contract_snapshot = required_policy_versions_for_contract(task_contract_version)
        expected_ids = {version.pk for version in expected_versions}
        supplied_ids = {version.pk for version in versions}
        if not has_contract_snapshot:
            raise ValidationError("A release evaluation requires at least one exact required contract policy link.")
        if len(supplied_ids) != len(versions) or supplied_ids != expected_ids:
            raise ValidationError(
                "A release evaluation must use the exact union of required contract policies and current mandatory policies."
            )
        authorization_blockers = _rule_evaluator_authorization_blockers(
            principal=initiated_by_principal,
            grant=permission_grant,
            task_contract_version=task_contract_version,
        )
        if acting_role != ActingRole.SYSTEM:
            authorization_blockers.append("RULE_EVALUATOR_ACTING_ROLE_NOT_SYSTEM")
        if authorization_blockers:
            raise ValidationError({"initiated_by_principal": sorted(set(authorization_blockers))})
        expected = sum(len(version.normalized_rules()) for version in versions)
        return self.create(
            publication=publication,
            policy_version_ids=sorted(str(version.pk) for version in versions),
            policy_set_sha256=policy_set_hash(versions),
            context_sha256=context_hash,
            expected_result_count=expected,
            evaluator_key=evaluator_key,
            command_id=command_id,
            payload_hash=payload_hash,
            initiated_by_principal=initiated_by_principal,
            initiated_by_acting_role=acting_role,
            initiated_under_grant=permission_grant,
            recorded_by_principal=recorded_by_principal,
        )


class RuleEvaluationRun(UUIDv7Model):
    class Status(models.TextChoices):
        RUNNING = "RUNNING", "Running"
        COMPLETED = "COMPLETED", "Completed"

    class Outcome(models.TextChoices):
        UNKNOWN = "UNKNOWN", "Unknown"
        PASSED = "PASSED", "Passed"
        BLOCKED = "BLOCKED", "Blocked"

    publication = models.ForeignKey(Publication, on_delete=models.PROTECT, related_name="evaluation_runs")
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.RUNNING)
    outcome = models.CharField(max_length=16, choices=Outcome.choices, default=Outcome.UNKNOWN)
    policy_version_ids = models.JSONField()
    policy_set_sha256 = models.CharField(max_length=64, validators=[validate_sha256])
    context_sha256 = models.CharField(max_length=64, validators=[validate_sha256])
    expected_result_count = models.PositiveIntegerField()
    actual_result_count = models.PositiveIntegerField(default=0)
    evaluator_key = models.CharField(max_length=100)
    command_id = models.UUIDField(unique=True)
    payload_hash = models.CharField(max_length=64, validators=[validate_sha256])
    initiated_by_principal = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")
    initiated_by_acting_role = models.CharField(max_length=24, choices=ActingRole.choices)
    initiated_under_grant = models.ForeignKey("accounts.PermissionGrant", on_delete=models.PROTECT, related_name="+")
    recorded_by_principal = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")
    started_at = models.DateTimeField(auto_now_add=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    objects = RuleEvaluationRunManager()

    def clean(self):
        super().clean()
        if not self.publication_id or not self.initiated_by_principal_id or not self.initiated_under_grant_id:
            return
        blockers = _rule_evaluator_authorization_blockers(
            principal=self.initiated_by_principal,
            grant=self.initiated_under_grant,
            task_contract_version=self.publication.submission.task.contract_version,
        )
        if self.initiated_by_acting_role != ActingRole.SYSTEM:
            blockers.append("RULE_EVALUATOR_ACTING_ROLE_NOT_SYSTEM")
        if blockers:
            raise ValidationError({"initiated_by_principal": sorted(set(blockers))})

    def save(self, *args, **kwargs):
        if self.pk:
            original = type(self).objects.filter(pk=self.pk).first()
            if original and original.status == self.Status.COMPLETED:
                raise ValidationError("A completed RuleEvaluationRun is immutable.")
        self.full_clean()
        return super().save(*args, **kwargs)

    def complete(self) -> RuleEvaluationRun:
        with transaction.atomic():
            run = type(self).objects.select_for_update().get(pk=self.pk)
            if run.status == self.Status.COMPLETED:
                return run
            results = list(run.results.select_related("policy_version"))
            expected: set[tuple[str, str]] = set()
            versions = PolicyVersion.objects.filter(pk__in=run.policy_version_ids)
            if versions.count() != len(run.policy_version_ids):
                raise ValidationError("The run's exact policy set is incomplete.")
            for version in versions:
                expected.update((str(version.pk), rule["rule_code"]) for rule in version.normalized_rules())
            actual = {(str(result.policy_version_id), result.rule_code) for result in results}
            if actual != expected or len(results) != run.expected_result_count:
                raise ValidationError("A run cannot complete until every exact policy rule has one result.")
            run.actual_result_count = len(results)
            run.outcome = (
                self.Outcome.PASSED
                if all((not result.required) or result.result == RuleEvaluationResult.Result.PASS for result in results)
                else self.Outcome.BLOCKED
            )
            run.status = self.Status.COMPLETED
            run.completed_at = timezone.now()
            run.save(update_fields=["actual_result_count", "outcome", "status", "completed_at"])
            self.__dict__.update(run.__dict__)
            return self


class RuleEvaluationResultManager(ImmutableManager):
    def record(self, **values) -> RuleEvaluationResult:
        existing = self.filter(command_id=values["command_id"]).first()
        if existing:
            if existing.payload_hash != values["payload_hash"]:
                raise ValidationError("The command_id was already used with a different payload.")
            return existing
        return self.create(**values)


class RuleEvaluationResult(ImmutableFact):
    class Result(models.TextChoices):
        PASS = "PASS", "Pass"
        FAIL = "FAIL", "Fail"
        BLOCKED = "BLOCKED", "Blocked"
        ERROR = "ERROR", "Error"
        SKIPPED = "SKIPPED", "Skipped"
        UNKNOWN = "UNKNOWN", "Unknown"

    evaluation_run = models.ForeignKey(RuleEvaluationRun, on_delete=models.PROTECT, related_name="results")
    policy_version = models.ForeignKey(PolicyVersion, on_delete=models.PROTECT, related_name="evaluation_results")
    rule_code = models.CharField(max_length=100)
    required = models.BooleanField(default=True)
    result = models.CharField(max_length=16, choices=Result.choices)
    detail = models.JSONField(default=dict, blank=True)
    evidence_reference = models.CharField(max_length=1024, blank=True)
    evidence_sha256 = models.CharField(max_length=64, validators=[validate_sha256], blank=True)
    command_id = models.UUIDField(unique=True)
    payload_hash = models.CharField(max_length=64, validators=[validate_sha256])
    evaluated_by_principal = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")
    recorded_by_principal = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")
    evaluated_at = models.DateTimeField(default=timezone.now)

    objects = RuleEvaluationResultManager()

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["evaluation_run", "policy_version", "rule_code"],
                name="releasegate_unique_rule_result",
            )
        ]

    def clean(self):
        super().clean()
        if self.evaluation_run_id:
            if self.evaluation_run.status != RuleEvaluationRun.Status.RUNNING:
                raise ValidationError({"evaluation_run": "Results cannot be added after run completion."})
            if str(self.policy_version_id) not in self.evaluation_run.policy_version_ids:
                raise ValidationError({"policy_version": "Policy version is not in the run's exact policy set."})
            if self.evaluated_by_principal_id != self.evaluation_run.initiated_by_principal_id:
                raise ValidationError(
                    {"evaluated_by_principal": "A result must be written by the run's exact trusted evaluator."}
                )
            blockers = _rule_evaluator_authorization_blockers(
                principal=self.evaluated_by_principal,
                grant=self.evaluation_run.initiated_under_grant,
                task_contract_version=self.evaluation_run.publication.submission.task.contract_version,
                at=self.evaluated_at,
            )
            if self.evaluation_run.initiated_by_acting_role != ActingRole.SYSTEM:
                blockers.append("RULE_EVALUATOR_ACTING_ROLE_NOT_SYSTEM")
            if blockers:
                raise ValidationError({"evaluated_by_principal": sorted(set(blockers))})
        if self.policy_version_id:
            expected = {rule["rule_code"]: rule["required"] for rule in self.policy_version.normalized_rules()}
            if self.rule_code not in expected:
                raise ValidationError({"rule_code": "Rule is not defined by this exact policy version."})
            if self.required != expected[self.rule_code]:
                raise ValidationError({"required": "Required flag must match the policy version."})


def _grant_blockers(grant, principal, acting_role, channel_account, task_contract_version, at) -> list[str]:
    from accounts.authorization import resolve_authorization

    blockers: list[str] = []
    if grant.principal_id != principal.pk:
        blockers.append("PUBLISHER_GRANT_PRINCIPAL_MISMATCH")
    if grant.action != "PUBLISH" or grant.effect != "ALLOW" or grant.grant_status != "ACTIVE":
        blockers.append("PUBLISHER_GRANT_NOT_ACTIVE_ALLOW_PUBLISH")
    if grant.valid_from > at or (grant.valid_until and grant.valid_until <= at):
        blockers.append("PUBLISHER_GRANT_EXPIRED_OR_NOT_YET_VALID")
    if principal.principal_status != "ACTIVE":
        blockers.append("PUBLISHER_NOT_ACTIVE")
    decision = resolve_authorization(
        principal=principal,
        acting_role=acting_role,
        action="PUBLISH",
        scope_kind="ACCOUNT",
        product=task_contract_version.product_profile_version.product_id,
        platform_code=channel_account.platform_code,
        account_ref=channel_account.account_code,
        at=at,
    )
    if not decision.allowed:
        blockers.append(f"PUBLISHER_AUTHORIZATION_{decision.reason}")
    elif decision.grant is None or decision.grant.pk != grant.pk:
        blockers.append("PUBLISHER_GRANT_NOT_CENTRALLY_RESOLVED")
    return blockers


def release_context_hash(
    *, publication, review_decision, primary_asset_version, task_contract_version, publisher_principal,
    publisher_grant, channel_account, runtime_environment, account_environment_binding, capability_state,
    policy_versions
) -> str:
    return canonical_sha256(
        {
            "publication_id": str(publication.pk),
            "submission_id": str(publication.submission_id),
            "review_decision_id": str(review_decision.pk),
            "review_decision_sha256": review_decision.decision_sha256,
            "primary_asset_version_id": str(primary_asset_version.pk),
            "asset_manifest_sha256": primary_asset_version.manifest_sha256,
            "task_contract_version_id": str(task_contract_version.pk),
            "contract_manifest_sha256": task_contract_version.manifest_sha256,
            "publisher_principal_id": str(publisher_principal.pk),
            "publisher_grant": {
                "id": str(publisher_grant.pk), "status": publisher_grant.grant_status,
                "updated_at": publisher_grant.updated_at.isoformat(), "valid_until": (
                    publisher_grant.valid_until.isoformat() if publisher_grant.valid_until else None
                ),
            },
            "channel_account": {
                "id": str(channel_account.pk), "status": channel_account.status,
                "updated_at": channel_account.updated_at.isoformat(),
            },
            "runtime_environment": {
                "id": str(runtime_environment.pk), "status": runtime_environment.status,
                "updated_at": runtime_environment.updated_at.isoformat(),
            },
            "account_environment_binding_id": str(account_environment_binding.pk),
            "capability_state_id": str(capability_state.pk),
            "policy_set_sha256": policy_set_hash(policy_versions),
        }
    )


class ReleaseGateRecordManager(ImmutableManager):
    def evaluate(self, **values) -> ReleaseGateRecord:
        command_id = values["command_id"]
        payload_hash = values["payload_hash"]
        existing = self.filter(command_id=command_id).first()
        if existing:
            if existing.payload_hash != payload_hash:
                raise ValidationError("The command_id was already used with a different payload.")
            return existing
        submitted_task = values["task_submission"].task
        current_task = type(submitted_task).objects.get(pk=values["task_submission"].task_id)
        evaluator = values["evaluated_by_principal"]
        evaluator_role = values["evaluated_by_acting_role"]
        if evaluator_role == ActingRole.SYSTEM:
            if evaluator.principal_type not in {"SERVICE_ACCOUNT", "SYSTEM"} or not evaluator.can_authenticate:
                raise ValidationError("SYSTEM gate evaluation requires an active trusted service principal.")
        else:
            evaluator.validate_acting_role(evaluator_role)
        guard_release_gate(
            current_task,
            submission=values["task_submission"],
            review_decision=values["review_decision"],
        )
        provisional = self.model(**values)
        provisional.policy_set_sha256 = values["evaluation_run"].policy_set_sha256
        provisional.context_sha256 = release_context_hash(
            publication=values["publication"], review_decision=values["review_decision"],
            primary_asset_version=values["primary_asset_version"], task_contract_version=values["task_contract_version"],
            publisher_principal=values["publisher_principal"], publisher_grant=values["publisher_grant"],
            channel_account=values["channel_account"], runtime_environment=values["runtime_environment"],
            account_environment_binding=values["account_environment_binding"], capability_state=values["capability_state"],
            policy_versions=PolicyVersion.objects.filter(pk__in=values["evaluation_run"].policy_version_ids),
        )
        blockers = provisional.current_blockers(include_gate_state=False)
        provisional.outcome = (
            ReleaseGateRecord.Outcome.BLOCKED if blockers else ReleaseGateRecord.Outcome.PASSED
        )
        provisional.failure_reasons = blockers
        provisional.valid_until = values.get("valid_until") or timezone.now() + timedelta(minutes=15)
        results = list(values["evaluation_run"].results.all())
        provisional.result_count = len(results)
        with transaction.atomic():
            provisional.save()
            for result in results:
                ReleaseGateEvaluationLink.objects.create(gate=provisional, evaluation_result=result)
        return provisional


class ReleaseGateRecord(ImmutableFact):
    class Outcome(models.TextChoices):
        PASSED = "PASSED", "Passed"
        BLOCKED = "BLOCKED", "Blocked"

    publication = models.ForeignKey(Publication, on_delete=models.PROTECT, related_name="gate_records")
    task_submission = models.ForeignKey("contentops.TaskSubmission", on_delete=models.PROTECT, related_name="gate_records")
    review_decision = models.ForeignKey("contentops.ReviewDecision", on_delete=models.PROTECT, related_name="gate_records")
    primary_asset_version = models.ForeignKey(
        "contentops.ContentAssetVersion", on_delete=models.PROTECT, related_name="gate_records"
    )
    task_contract_version = models.ForeignKey(
        "workflow.TaskContractVersion", on_delete=models.PROTECT, related_name="gate_records"
    )
    publisher_principal = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")
    publisher_grant = models.ForeignKey("accounts.PermissionGrant", on_delete=models.PROTECT, related_name="+")
    channel_account = models.ForeignKey(ChannelAccount, on_delete=models.PROTECT, related_name="gate_records")
    runtime_environment = models.ForeignKey(RuntimeEnvironment, on_delete=models.PROTECT, related_name="gate_records")
    account_environment_binding = models.ForeignKey(
        AccountEnvironmentBinding, on_delete=models.PROTECT, related_name="gate_records"
    )
    capability_state = models.ForeignKey(CapabilityState, on_delete=models.PROTECT, related_name="gate_records")
    evaluation_run = models.OneToOneField(RuleEvaluationRun, on_delete=models.PROTECT, related_name="gate_record")
    policy_set_sha256 = models.CharField(max_length=64, validators=[validate_sha256])
    context_sha256 = models.CharField(max_length=64, validators=[validate_sha256])
    outcome = models.CharField(max_length=16, choices=Outcome.choices)
    failure_reasons = models.JSONField(default=list, blank=True)
    result_count = models.PositiveIntegerField()
    valid_until = models.DateTimeField()
    command_id = models.UUIDField(unique=True)
    payload_hash = models.CharField(max_length=64, validators=[validate_sha256])
    evaluated_by_principal = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")
    evaluated_by_acting_role = models.CharField(max_length=24, choices=ActingRole.choices)
    recorded_by_principal = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")
    evaluated_at = models.DateTimeField(default=timezone.now)

    objects = ReleaseGateRecordManager()

    def clean(self):
        super().clean()
        if self.publication_id and self.task_submission_id and self.publication.submission_id != self.task_submission_id:
            raise ValidationError("Gate must bind the Publication's exact TaskSubmission.")
        if self.review_decision_id and (
            self.review_decision.submission_id != self.task_submission_id or self.review_decision.decision != "APPROVED"
        ):
            raise ValidationError("Gate requires the exact APPROVED ReviewDecision.")
        if self.publication_id and (
            self.publisher_principal_id != self.publication.requested_by_principal_id
            or self.publisher_grant_id != self.publication.requested_under_grant_id
        ):
            raise ValidationError("Gate must use the Publication intent's exact Publisher and Grant.")
        if self.primary_asset_version_id and self.primary_asset_version_id != self.task_submission.primary_asset_version_id:
            raise ValidationError("Gate must bind the Submission's exact primary ContentAssetVersion.")
        if self.task_contract_version_id and self.task_submission.task.contract_version_id != self.task_contract_version_id:
            raise ValidationError("Gate must bind the Submission Task's exact TaskContractVersion.")
        if self.evaluation_run_id and (
            self.evaluation_run.publication_id != self.publication_id
            or self.evaluation_run.status != RuleEvaluationRun.Status.COMPLETED
            or self.evaluation_run.actual_result_count != self.evaluation_run.expected_result_count
        ):
            raise ValidationError("Gate requires this Publication's complete RuleEvaluationRun and Results.")
        if self.account_environment_binding_id and (
            self.account_environment_binding.channel_account_id != self.channel_account_id
            or self.account_environment_binding.runtime_environment_id != self.runtime_environment_id
        ):
            raise ValidationError("Gate account/environment binding does not match its exact account and environment.")
        if self.capability_state_id and (
            self.capability_state.account_environment_binding_id != self.account_environment_binding_id
        ):
            raise ValidationError("Gate capability does not belong to the exact binding.")
        if self.outcome == self.Outcome.PASSED and self.failure_reasons:
            raise ValidationError("A PASSED Gate cannot contain fail-closed reasons.")

    def current_blockers(self, *, at=None, include_gate_state=True) -> list[str]:
        at = at or timezone.now()
        blockers: list[str] = []
        required_policies, has_contract_snapshot = required_policy_versions_for_contract(
            self.task_contract_version,
            at=at,
        )
        if not has_contract_snapshot:
            blockers.append("CONTRACT_REQUIRED_POLICY_SET_MISSING")
        if not required_policies or policy_set_hash(required_policies) != self.policy_set_sha256:
            blockers.append("REQUIRED_POLICY_SET_MISSING_OR_STALE")
        current_context = release_context_hash(
            publication=self.publication,
            review_decision=self.review_decision,
            primary_asset_version=self.primary_asset_version,
            task_contract_version=self.task_contract_version,
            publisher_principal=self.publisher_principal,
            publisher_grant=self.publisher_grant,
            channel_account=self.channel_account,
            runtime_environment=self.runtime_environment,
            account_environment_binding=self.account_environment_binding,
            capability_state=self.capability_state,
            policy_versions=required_policies,
        )
        if current_context != self.context_sha256:
            blockers.append("RELEASE_CONTEXT_CHANGED")
        if self.channel_account.status != ChannelAccount.Status.ACTIVE:
            blockers.append("CHANNEL_ACCOUNT_NOT_ACTIVE")
        if self.runtime_environment.status != RuntimeEnvironment.Status.ACTIVE:
            blockers.append("RUNTIME_ENVIRONMENT_NOT_ACTIVE")
        if not self.account_environment_binding.is_current_at(at):
            blockers.append("ACCOUNT_ENVIRONMENT_BINDING_NOT_CURRENT")
        if (
            self.capability_state.capability_code != CapabilityState.MANUAL_PUBLISH
            or not self.capability_state.is_current_open_at(at)
        ):
            blockers.append("MANUAL_PUBLISH_CAPABILITY_NOT_OPEN")
        blockers.extend(
            _grant_blockers(
                self.publisher_grant, self.publisher_principal,
                self.publication.requested_by_acting_role, self.channel_account,
                self.task_contract_version, at,
            )
        )
        evaluator_blockers = _rule_evaluator_authorization_blockers(
            principal=self.evaluation_run.initiated_by_principal,
            grant=self.evaluation_run.initiated_under_grant,
            task_contract_version=self.task_contract_version,
            at=at,
        )
        if self.evaluation_run.initiated_by_acting_role != ActingRole.SYSTEM:
            evaluator_blockers.append("RULE_EVALUATOR_ACTING_ROLE_NOT_SYSTEM")
        blockers.extend(evaluator_blockers)
        if self.evaluation_run.status != RuleEvaluationRun.Status.COMPLETED:
            blockers.append("RULE_EVALUATION_NOT_COMPLETE")
        if self.evaluation_run.outcome != RuleEvaluationRun.Outcome.PASSED:
            blockers.append("RULE_EVALUATION_NOT_PASSED")
        if self.evaluation_run.policy_set_sha256 != self.policy_set_sha256:
            blockers.append("RULE_EVALUATION_POLICY_SET_MISMATCH")
        if self.evaluation_run.context_sha256 != self.context_sha256:
            blockers.append("RULE_EVALUATION_CONTEXT_MISMATCH")
        if self.evaluation_run.actual_result_count != self.evaluation_run.expected_result_count:
            blockers.append("RULE_EVALUATION_RESULT_SET_INCOMPLETE")
        if include_gate_state:
            if self.outcome != self.Outcome.PASSED:
                blockers.append("GATE_NOT_PASSED")
            if self.valid_until <= at:
                blockers.append("GATE_EXPIRED")
            if self.evaluation_links.count() != self.result_count or self.result_count != self.evaluation_run.actual_result_count:
                blockers.append("GATE_EVALUATION_LINKS_INCOMPLETE")
        return sorted(set(blockers))


class ReleaseGateEvaluationLink(ImmutableFact):
    gate = models.ForeignKey(ReleaseGateRecord, on_delete=models.PROTECT, related_name="evaluation_links")
    evaluation_result = models.OneToOneField(
        RuleEvaluationResult, on_delete=models.PROTECT, related_name="gate_link"
    )
    created_at = models.DateTimeField(auto_now_add=True)

    def clean(self):
        super().clean()
        if self.gate_id and self.evaluation_result_id and self.evaluation_result.evaluation_run_id != self.gate.evaluation_run_id:
            raise ValidationError("A Gate link must bind a Result from the Gate's exact evaluation run.")


class PublicationEventManager(ImmutableManager):
    def append(
        self, *, publication, event_type, command_id, payload_hash, expected_state_version,
        actor_principal, acting_role, permission_grant, recorded_by_principal, release_gate=None,
        external_publication_id="", external_url="", proof_reference="", proof_sha256=""
    ) -> PublicationEvent:
        existing = self.filter(command_id=command_id).first()
        if existing:
            if existing.payload_hash != payload_hash:
                raise ValidationError("The command_id was already used with a different payload.")
            return existing
        actor_principal.validate_acting_role(acting_role)
        with transaction.atomic():
            current = Publication.objects.select_for_update().get(pk=publication.pk)
            if current.state_version != expected_state_version:
                raise ValidationError("Publication version conflict; reread before retrying.")
            target = {
                PublicationEvent.EventType.GATE_PENDING: Publication.Status.GATE_PENDING,
                PublicationEvent.EventType.GATE_BLOCKED: Publication.Status.GATE_BLOCKED,
                PublicationEvent.EventType.READY_FOR_MANUAL_PUBLISH: Publication.Status.READY_FOR_MANUAL_PUBLISH,
                PublicationEvent.EventType.MANUAL_PUBLISHED_RECORDED: Publication.Status.MANUAL_PUBLISHED_RECORDED,
            }[event_type]
            if event_type == PublicationEvent.EventType.GATE_PENDING:
                if current.state_version and current.status not in {
                    Publication.Status.GATE_BLOCKED, Publication.Status.READY_FOR_MANUAL_PUBLISH
                }:
                    raise ValidationError("GATE_PENDING may only initialize or recover a blocked/stale Publication.")
                release_gate = release_gate or current.current_gate
            elif event_type == PublicationEvent.EventType.GATE_BLOCKED:
                if current.status != Publication.Status.GATE_PENDING or not release_gate or release_gate.outcome != "BLOCKED":
                    raise ValidationError("GATE_BLOCKED requires an exact BLOCKED Gate from GATE_PENDING.")
            elif event_type == PublicationEvent.EventType.READY_FOR_MANUAL_PUBLISH:
                guard_release_gate(
                    current.submission.task,
                    submission=current.submission,
                    review_decision=release_gate.review_decision if release_gate else None,
                )
                if current.status != Publication.Status.GATE_PENDING or not release_gate:
                    raise ValidationError("READY requires an exact Gate from GATE_PENDING.")
                if release_gate.publication_id != current.pk or release_gate.current_blockers():
                    raise ValidationError("READY is fail-closed: the exact Gate is missing, stale, expired, or blocked.")
                latest_gate = current.gate_records.order_by("-evaluated_at", "-id").first()
                if latest_gate.pk != release_gate.pk or self.filter(
                    release_gate=release_gate,
                    event_type__in=[self.model.EventType.READY_FOR_MANUAL_PUBLISH,
                                   self.model.EventType.MANUAL_PUBLISHED_RECORDED],
                ).exists():
                    raise ValidationError("An old or already-used Gate cannot be reused.")
                last_pending = current.events.filter(event_type=self.model.EventType.GATE_PENDING).order_by("-event_sequence").first()
                if last_pending and release_gate.evaluated_at <= last_pending.occurred_at:
                    raise ValidationError("Recovery requires a new Gate evaluated after the latest GATE_PENDING event.")
                if actor_principal.pk != release_gate.publisher_principal_id or permission_grant.pk != release_gate.publisher_grant_id:
                    raise ValidationError("READY must use the Gate's exact authorized Publisher and Grant.")
            else:
                guard_manual_publication(current.submission.task, publication=current)
                if (
                    release_gate is None
                    or current.status != Publication.Status.READY_FOR_MANUAL_PUBLISH
                    or current.current_gate_id != release_gate.pk
                ):
                    raise ValidationError("Manual publication proof requires the current READY Gate.")
                if release_gate.current_blockers():
                    raise ValidationError("Current context changed; return to GATE_PENDING and evaluate a new Gate.")
                if actor_principal.pk != release_gate.publisher_principal_id or permission_grant.pk != release_gate.publisher_grant_id:
                    raise ValidationError("Publication proof must name the Gate's exact Publisher and Grant.")
                if not (external_publication_id or external_url) or not proof_reference or not proof_sha256:
                    raise ValidationError("Manual publication requires external ID/URL and exact proof reference/hash.")
            previous = current.events.order_by("-event_sequence").first()
            event = self.model(
                publication=current, event_type=event_type, release_gate=release_gate,
                event_sequence=current.state_version + 1, previous_event=previous,
                expected_state_version=current.state_version, resulting_state_version=current.state_version + 1,
                command_id=command_id, payload_hash=payload_hash, actor_principal=actor_principal,
                acting_role=acting_role, permission_grant=permission_grant,
                recorded_by_principal=recorded_by_principal, external_publication_id=external_publication_id,
                external_url=external_url, proof_reference=proof_reference, proof_sha256=proof_sha256,
            )
            event.save()
            Publication.objects.filter(pk=current.pk).update(
                status=target, state_version=current.state_version + 1,
                current_gate=release_gate if target != Publication.Status.GATE_PENDING else None,
            )
            return event


class PublicationEvent(ImmutableFact):
    class EventType(models.TextChoices):
        GATE_PENDING = "GATE_PENDING", "Gate pending"
        GATE_BLOCKED = "GATE_BLOCKED", "Gate blocked"
        READY_FOR_MANUAL_PUBLISH = "READY_FOR_MANUAL_PUBLISH", "Ready for manual publish"
        MANUAL_PUBLISHED_RECORDED = "MANUAL_PUBLISHED_RECORDED", "Manual publication recorded"

    publication = models.ForeignKey(Publication, on_delete=models.PROTECT, related_name="events")
    event_type = models.CharField(max_length=32, choices=EventType.choices)
    release_gate = models.ForeignKey(
        ReleaseGateRecord, null=True, blank=True, on_delete=models.PROTECT, related_name="publication_events"
    )
    event_sequence = models.PositiveIntegerField()
    previous_event = models.OneToOneField(
        "self", null=True, blank=True, on_delete=models.PROTECT, related_name="next_event"
    )
    expected_state_version = models.PositiveIntegerField()
    resulting_state_version = models.PositiveIntegerField()
    command_id = models.UUIDField(unique=True)
    payload_hash = models.CharField(max_length=64, validators=[validate_sha256])
    actor_principal = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")
    acting_role = models.CharField(max_length=24, choices=ActingRole.choices)
    permission_grant = models.ForeignKey("accounts.PermissionGrant", on_delete=models.PROTECT, related_name="+")
    recorded_by_principal = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")
    external_publication_id = models.CharField(max_length=255, blank=True)
    external_url = models.URLField(max_length=1024, blank=True)
    proof_reference = models.CharField(max_length=1024, blank=True)
    proof_sha256 = models.CharField(max_length=64, validators=[validate_sha256], blank=True)
    occurred_at = models.DateTimeField(default=timezone.now)

    objects = PublicationEventManager()

    class Meta:
        ordering = ["publication_id", "event_sequence"]
        constraints = [
            models.UniqueConstraint(
                fields=["publication", "event_sequence"], name="releasegate_unique_publication_event_sequence"
            ),
            models.CheckConstraint(condition=Q(event_sequence__gte=1), name="releasegate_event_sequence_gte_one"),
            models.CheckConstraint(
                condition=Q(resulting_state_version=models.F("expected_state_version") + 1),
                name="releasegate_event_advances_one_version",
            ),
        ]

    def clean(self):
        super().clean()
        if self.event_sequence == 1 and self.previous_event_id:
            raise ValidationError("The first PublicationEvent cannot have a previous event.")
        if self.event_sequence > 1 and (
            not self.previous_event_id
            or self.previous_event.publication_id != self.publication_id
            or self.previous_event.event_sequence != self.event_sequence - 1
        ):
            raise ValidationError("PublicationEvent must append to the exact prior event.")
        if self.release_gate_id and self.release_gate.publication_id != self.publication_id:
            raise ValidationError("PublicationEvent Gate belongs to another Publication.")
