from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from django.core.exceptions import PermissionDenied
from django.db.models import Q
from django.utils import timezone

from accounts.models import PermissionGrant, Principal


@dataclass(frozen=True, slots=True)
class AuthorizationDecision:
    allowed: bool
    reason: str
    principal: Principal | None = None
    grant: PermissionGrant | None = None
    denied_by: PermissionGrant | None = None


def _persisted_principal(principal: Principal) -> Principal | None:
    if not getattr(principal, "pk", None):
        return None
    return Principal.objects.filter(pk=principal.pk).first()


def validate_acting_role(principal: Principal, acting_role: str) -> Principal:
    """Return the persisted Principal only when the claimed role is genuine."""

    persisted = _persisted_principal(principal)
    if persisted is None:
        raise PermissionDenied("UNKNOWN_PRINCIPAL")
    if not persisted.can_authenticate:
        raise PermissionDenied("INACTIVE_PRINCIPAL")
    if acting_role not in Principal.Role.values or persisted.role != acting_role:
        raise PermissionDenied("ACTING_ROLE_MISMATCH")
    return persisted


def _scope_query(
    scope_kind: str,
    *,
    product: Any = None,
    platform_code: str = "",
    account_ref: str = "",
    surface_ref: str = "",
) -> tuple[Q | None, tuple[str, ...], str | None]:
    supplied = {
        PermissionGrant.ScopeKind.PRODUCT: product is not None,
        PermissionGrant.ScopeKind.PLATFORM: bool(platform_code),
        PermissionGrant.ScopeKind.ACCOUNT: bool(account_ref),
        PermissionGrant.ScopeKind.SURFACE: bool(surface_ref),
    }
    if scope_kind == PermissionGrant.ScopeKind.GLOBAL:
        if any(supplied.values()):
            return None, (), "INVALID_SCOPE"
        return Q(scope_kind=PermissionGrant.ScopeKind.GLOBAL), (PermissionGrant.ScopeKind.GLOBAL,), None

    if scope_kind not in supplied or not supplied[scope_kind]:
        return None, (), "INVALID_SCOPE"

    allowed_contexts = {
        PermissionGrant.ScopeKind.PRODUCT: {PermissionGrant.ScopeKind.PRODUCT},
        PermissionGrant.ScopeKind.PLATFORM: {PermissionGrant.ScopeKind.PLATFORM},
        PermissionGrant.ScopeKind.ACCOUNT: {
            PermissionGrant.ScopeKind.PRODUCT,
            PermissionGrant.ScopeKind.PLATFORM,
            PermissionGrant.ScopeKind.ACCOUNT,
        },
        PermissionGrant.ScopeKind.SURFACE: {
            PermissionGrant.ScopeKind.PRODUCT,
            PermissionGrant.ScopeKind.PLATFORM,
            PermissionGrant.ScopeKind.ACCOUNT,
            PermissionGrant.ScopeKind.SURFACE,
        },
    }[scope_kind]
    if any(present and kind not in allowed_contexts for kind, present in supplied.items()):
        return None, (), "INVALID_SCOPE"

    query = Q(scope_kind=PermissionGrant.ScopeKind.GLOBAL)
    applicable_kinds = [PermissionGrant.ScopeKind.GLOBAL]
    if supplied[PermissionGrant.ScopeKind.PRODUCT]:
        query |= Q(
            scope_kind=PermissionGrant.ScopeKind.PRODUCT,
            product_id=getattr(product, "pk", product),
        )
        applicable_kinds.append(PermissionGrant.ScopeKind.PRODUCT)
    if supplied[PermissionGrant.ScopeKind.PLATFORM]:
        query |= Q(scope_kind=PermissionGrant.ScopeKind.PLATFORM, platform_code=platform_code)
        applicable_kinds.append(PermissionGrant.ScopeKind.PLATFORM)
    if supplied[PermissionGrant.ScopeKind.ACCOUNT]:
        query |= Q(scope_kind=PermissionGrant.ScopeKind.ACCOUNT, account_ref=account_ref)
        applicable_kinds.append(PermissionGrant.ScopeKind.ACCOUNT)
    if supplied[PermissionGrant.ScopeKind.SURFACE]:
        query |= Q(scope_kind=PermissionGrant.ScopeKind.SURFACE, surface_ref=surface_ref)
        applicable_kinds.append(PermissionGrant.ScopeKind.SURFACE)
    return query, tuple(applicable_kinds), None


def resolve_authorization(
    *,
    principal: Principal,
    acting_role: str,
    action: str,
    scope_kind: str = PermissionGrant.ScopeKind.GLOBAL,
    product: Any = None,
    platform_code: str = "",
    account_ref: str = "",
    surface_ref: str = "",
    at: datetime | None = None,
) -> AuthorizationDecision:
    """Resolve one permission request fail-closed, with any applicable DENY winning."""

    persisted = _persisted_principal(principal)
    if persisted is None:
        return AuthorizationDecision(False, "UNKNOWN_PRINCIPAL")
    if not persisted.can_authenticate:
        return AuthorizationDecision(False, "INACTIVE_PRINCIPAL", principal=persisted)
    if acting_role not in Principal.Role.values or persisted.role != acting_role:
        return AuthorizationDecision(False, "ACTING_ROLE_MISMATCH", principal=persisted)
    if action not in PermissionGrant.Action.values:
        return AuthorizationDecision(False, "UNKNOWN_ACTION", principal=persisted)

    applicable_scope, applicable_kinds, scope_error = _scope_query(
        scope_kind,
        product=product,
        platform_code=platform_code,
        account_ref=account_ref,
        surface_ref=surface_ref,
    )
    if scope_error:
        return AuthorizationDecision(False, scope_error, principal=persisted)

    instant = at or timezone.now()
    grants = PermissionGrant.objects.filter(
        principal=persisted,
        action=action,
        grant_status=PermissionGrant.GrantStatus.ACTIVE,
        valid_from__lte=instant,
    ).filter(Q(valid_until__isnull=True) | Q(valid_until__gt=instant)).filter(applicable_scope)

    deny = grants.filter(effect=PermissionGrant.Effect.DENY).order_by("created_at", "id").first()
    if deny is not None:
        return AuthorizationDecision(False, "DENY_GRANT", principal=persisted, denied_by=deny)

    allows = grants.filter(effect=PermissionGrant.Effect.ALLOW)
    allow = None
    specificity = (
        PermissionGrant.ScopeKind.SURFACE,
        PermissionGrant.ScopeKind.ACCOUNT,
        PermissionGrant.ScopeKind.PLATFORM,
        PermissionGrant.ScopeKind.PRODUCT,
        PermissionGrant.ScopeKind.GLOBAL,
    )
    for kind in specificity:
        if kind in applicable_kinds:
            allow = allows.filter(scope_kind=kind).order_by("created_at", "id").first()
            if allow is not None:
                break
    if allow is None:
        return AuthorizationDecision(False, "NO_ALLOW_GRANT", principal=persisted)
    return AuthorizationDecision(True, "ALLOW_GRANT", principal=persisted, grant=allow)


def require_authorization(**kwargs) -> PermissionGrant:
    decision = resolve_authorization(**kwargs)
    if not decision.allowed or decision.grant is None:
        raise PermissionDenied(decision.reason)
    return decision.grant
