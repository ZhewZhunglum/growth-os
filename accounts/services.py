from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.utils import timezone

from accounts.authorization import resolve_authorization
from accounts.models import PermissionGrant, Principal


@dataclass(frozen=True)
class GrantScope:
    scope_kind: str
    product_id: object | None = None
    platform_code: str = ""
    account_ref: str = ""
    surface_ref: str = ""


def _require_manage_account(actor: Principal) -> PermissionGrant:
    decision = resolve_authorization(
        principal=actor,
        acting_role=actor.role,
        action=PermissionGrant.Action.MANAGE_ACCOUNT,
        scope_kind=PermissionGrant.ScopeKind.GLOBAL,
    )
    if not decision.allowed or decision.grant is None:
        raise PermissionDenied("An exact active GLOBAL MANAGE_ACCOUNT grant is required.")
    return decision.grant


def _validate_grant_boundary(*, actor: Principal, principal: Principal, action: str) -> None:
    if actor.role == Principal.Role.OPERATOR:
        raise PermissionDenied("Operators cannot manage PermissionGrants.")
    if actor.role == Principal.Role.OPERATIONS_ADMIN:
        if principal.role != Principal.Role.OPERATOR:
            raise PermissionDenied("Operations Admin may manage only Operator accounts.")
        forbidden = {
            PermissionGrant.Action.PUBLISH,
            PermissionGrant.Action.MANAGE_ACCOUNT,
            PermissionGrant.Action.EMERGENCY_STOP,
        }
        if action in forbidden:
            raise PermissionDenied("Only Owner can grant this high-risk action.")
    if action == PermissionGrant.Action.PUBLISH and actor.role != Principal.Role.OWNER:
        raise PermissionDenied("PUBLISH can be granted only by Owner.")


@transaction.atomic
def issue_permission_grant(
    *,
    actor: Principal,
    principal: Principal,
    scope: GrantScope,
    action: str,
    effect: str,
    risk_level: str,
    valid_from: datetime,
    valid_until: datetime | None,
    supersedes_grant: PermissionGrant | None = None,
) -> PermissionGrant:
    _require_manage_account(actor)
    _validate_grant_boundary(actor=actor, principal=principal, action=action)
    if action == PermissionGrant.Action.PUBLISH:
        if scope.scope_kind != PermissionGrant.ScopeKind.ACCOUNT or not scope.account_ref:
            raise ValidationError("PUBLISH must be scoped to one exact ChannelAccount reference.")
        # An arbitrary string is not an account boundary.  Resolve the exact
        # active runtime account before minting this high-risk grant so a typo,
        # retired account, or made-up reference can never become publish
        # authority.
        from releasegate.models import ChannelAccount

        if not ChannelAccount.objects.filter(
            account_code=scope.account_ref,
            status=ChannelAccount.Status.ACTIVE,
        ).exists():
            raise ValidationError("PUBLISH requires one exact active ChannelAccount.")
        if risk_level not in {PermissionGrant.RiskLevel.HIGH, PermissionGrant.RiskLevel.CRITICAL}:
            raise ValidationError("PUBLISH requires HIGH or CRITICAL risk classification.")
        if valid_until is None:
            raise ValidationError("PUBLISH requires an explicit expiry time.")

    grant = PermissionGrant(
        principal=principal,
        scope_kind=scope.scope_kind,
        product_id=scope.product_id,
        platform_code=scope.platform_code,
        account_ref=scope.account_ref,
        surface_ref=scope.surface_ref,
        action=action,
        effect=effect,
        risk_level=risk_level,
        valid_from=valid_from,
        valid_until=valid_until,
        grant_status=PermissionGrant.GrantStatus.ACTIVE,
        granted_by_principal=actor,
        supersedes_grant=supersedes_grant,
    )
    grant.save()
    return grant


@transaction.atomic
def renew_permission_grant(
    *,
    actor: Principal,
    grant_id,
    valid_from: datetime,
    valid_until: datetime | None,
) -> PermissionGrant:
    original = PermissionGrant.objects.select_for_update().select_related("principal").get(pk=grant_id)
    return issue_permission_grant(
        actor=actor,
        principal=original.principal,
        scope=GrantScope(
            scope_kind=original.scope_kind,
            product_id=original.product_id,
            platform_code=original.platform_code,
            account_ref=original.account_ref,
            surface_ref=original.surface_ref,
        ),
        action=original.action,
        effect=original.effect,
        risk_level=original.risk_level,
        valid_from=valid_from,
        valid_until=valid_until,
        supersedes_grant=original,
    )


@transaction.atomic
def revoke_permission_grant(
    *,
    actor: Principal,
    grant_id,
    reason: str,
    revoked_at: datetime | None = None,
) -> PermissionGrant:
    _require_manage_account(actor)
    grant = PermissionGrant.objects.select_for_update().select_related("principal").get(pk=grant_id)
    _validate_grant_boundary(actor=actor, principal=grant.principal, action=grant.action)
    if grant.grant_status != PermissionGrant.GrantStatus.ACTIVE:
        raise ValidationError("Only an ACTIVE grant can be revoked.")
    if not reason.strip():
        raise ValidationError("Revocation reason is required.")
    grant.grant_status = PermissionGrant.GrantStatus.REVOKED
    grant.revoked_at = revoked_at or timezone.now()
    grant.revoked_by_principal = actor
    grant.revocation_reason = reason.strip()
    grant.save(
        update_fields=[
            "grant_status",
            "revoked_at",
            "revoked_by_principal",
            "revocation_reason",
            "updated_at",
        ]
    )
    return grant
