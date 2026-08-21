from __future__ import annotations

from django.contrib import messages
from django.contrib.auth import update_session_auth_hash
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.db.models import Count, Q
from django.http import Http404, HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from accounts.authorization import resolve_authorization
from accounts.models import PermissionGrant, Principal
from accounts.services import (
    GrantScope,
    issue_permission_grant,
    renew_permission_grant,
    revoke_permission_grant,
)
from dashboard.team_forms import (
    GrantIssueForm,
    GrantRenewForm,
    GrantRevokeForm,
    SelfPasswordChangeForm,
)


def _require_team_management(actor: Principal) -> PermissionGrant:
    decision = resolve_authorization(
        principal=actor,
        acting_role=actor.role,
        action=PermissionGrant.Action.MANAGE_ACCOUNT,
        scope_kind=PermissionGrant.ScopeKind.GLOBAL,
    )
    if not decision.allowed or decision.grant is None:
        raise PermissionDenied("需要一条当前有效的全局员工管理权限。")
    if actor.role == Principal.Role.OPERATOR:
        raise PermissionDenied("执行人员不能管理员工或权限。")
    return decision.grant


def _manageable_staff(actor: Principal):
    _require_team_management(actor)
    staff = Principal.objects.filter(principal_type=Principal.PrincipalType.HUMAN_USER)
    if actor.role == Principal.Role.OWNER:
        return staff.order_by("role", "username")
    if actor.role == Principal.Role.OPERATIONS_ADMIN:
        return staff.filter(role=Principal.Role.OPERATOR).order_by("username")
    raise PermissionDenied("当前账号不能管理员工。")


def _manageable_member(actor: Principal, member_id) -> Principal:
    member = _manageable_staff(actor).filter(pk=member_id).first()
    if member is None:
        # Do not reveal whether an out-of-scope identity exists.
        raise Http404("找不到该员工。")
    return member


def _grant_rows(member: Principal):
    now = timezone.now()
    grants = member.permission_grants.select_related(
        "product", "granted_by_principal", "revoked_by_principal", "supersedes_grant"
    ).order_by("-created_at", "-id")
    active = grants.filter(
        grant_status=PermissionGrant.GrantStatus.ACTIVE,
        valid_from__lte=now,
    ).filter(Q(valid_until__isnull=True) | Q(valid_until__gt=now))
    active_ids = set(active.values_list("pk", flat=True))
    return [(grant, grant.pk in active_ids) for grant in grants]


def _member_context(
    *,
    actor: Principal,
    member: Principal,
    issue_form: GrantIssueForm | None = None,
    renew_form: GrantRenewForm | None = None,
    revoke_form: GrantRevokeForm | None = None,
    selected_grant: PermissionGrant | None = None,
):
    return {
        "member": member,
        "grant_rows": _grant_rows(member),
        "issue_form": issue_form or GrantIssueForm(actor=actor),
        "renew_form": renew_form,
        "revoke_form": revoke_form,
        "selected_grant": selected_grant,
        "can_manage_high_risk": actor.role == Principal.Role.OWNER,
    }


@login_required
def team_members(request: HttpRequest) -> HttpResponse:
    staff = list(_manageable_staff(request.user))
    now = timezone.now()
    current_counts = {
        row["principal_id"]: row["count"]
        for row in PermissionGrant.objects.filter(
            principal__in=staff,
            grant_status=PermissionGrant.GrantStatus.ACTIVE,
            valid_from__lte=now,
        )
        .filter(Q(valid_until__isnull=True) | Q(valid_until__gt=now))
        .values("principal_id")
        .annotate(count=Count("id"))
    }
    rows = [(member, current_counts.get(member.pk, 0)) for member in staff]
    return render(request, "dashboard/team_members.html", {"staff_rows": rows})


@login_required
def team_member_detail(request: HttpRequest, member_id) -> HttpResponse:
    member = _manageable_member(request.user, member_id)
    return render(request, "dashboard/team_member_detail.html", _member_context(actor=request.user, member=member))


@login_required
@require_POST
def team_grant_issue(request: HttpRequest, member_id) -> HttpResponse:
    member = _manageable_member(request.user, member_id)
    form = GrantIssueForm(request.POST, actor=request.user)
    if form.is_valid():
        data = form.cleaned_data
        account = data["channel_account"]
        try:
            issue_permission_grant(
                actor=request.user,
                principal=member,
                scope=GrantScope(
                    scope_kind=data["scope_kind"],
                    product_id=data["product"].pk if data["product"] else None,
                    platform_code=data["platform_code"].strip(),
                    account_ref=account.account_code if account else "",
                    surface_ref=data["surface_ref"].strip(),
                ),
                action=data["action"],
                effect=data["effect"],
                risk_level=data["risk_level"],
                valid_from=data["valid_from"],
                valid_until=data["valid_until"],
            )
        except (PermissionDenied, ValidationError) as error:
            form.add_error(None, error)
        else:
            messages.success(request, "权限已发放，并已记录授权人和有效期。")
            return redirect("dashboard:team-member-detail", member_id=member.pk)
    return render(
        request,
        "dashboard/team_member_detail.html",
        _member_context(actor=request.user, member=member, issue_form=form),
        status=400,
    )


def _managed_grant(actor: Principal, grant_id) -> PermissionGrant:
    grant = get_object_or_404(PermissionGrant.objects.select_related("principal"), pk=grant_id)
    _manageable_member(actor, grant.principal_id)
    return grant


@login_required
@require_POST
def team_grant_renew(request: HttpRequest, grant_id) -> HttpResponse:
    grant = _managed_grant(request.user, grant_id)
    form = GrantRenewForm(request.POST, grant=grant)
    if form.is_valid():
        try:
            renew_permission_grant(
                actor=request.user,
                grant_id=grant.pk,
                valid_from=form.cleaned_data["valid_from"],
                valid_until=form.cleaned_data["valid_until"],
            )
        except (PermissionDenied, ValidationError) as error:
            form.add_error(None, error)
        else:
            messages.success(request, "已生成一条新的续期权限，旧记录保持不变。")
            return redirect("dashboard:team-member-detail", member_id=grant.principal_id)
    return render(
        request,
        "dashboard/team_member_detail.html",
        _member_context(
            actor=request.user,
            member=grant.principal,
            renew_form=form,
            selected_grant=grant,
        ),
        status=400,
    )


@login_required
@require_POST
def team_grant_revoke(request: HttpRequest, grant_id) -> HttpResponse:
    grant = _managed_grant(request.user, grant_id)
    form = GrantRevokeForm(request.POST)
    if form.is_valid():
        try:
            revoke_permission_grant(
                actor=request.user,
                grant_id=grant.pk,
                reason=form.cleaned_data["reason"],
            )
        except (PermissionDenied, ValidationError) as error:
            form.add_error(None, error)
        else:
            messages.success(request, "权限已撤销，原授权范围和时间记录仍保留。")
            return redirect("dashboard:team-member-detail", member_id=grant.principal_id)
    return render(
        request,
        "dashboard/team_member_detail.html",
        _member_context(
            actor=request.user,
            member=grant.principal,
            revoke_form=form,
            selected_grant=grant,
        ),
        status=400,
    )


@login_required
def change_my_password(request: HttpRequest) -> HttpResponse:
    if request.method == "POST":
        form = SelfPasswordChangeForm(request.POST, user=request.user)
        if form.is_valid():
            user = form.save()
            # Django's password hash is part of every authenticated session.
            # Refreshing only this session keeps it alive and invalidates the
            # user's other sessions without enumerating or logging credentials.
            update_session_auth_hash(request, user)
            messages.success(request, "密码已修改，其他设备上的旧登录已经失效。")
            return redirect("dashboard:change-my-password")
    else:
        form = SelfPasswordChangeForm(user=request.user)
    return render(request, "dashboard/change_password.html", {"form": form})
