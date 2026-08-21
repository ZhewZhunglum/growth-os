from __future__ import annotations

from datetime import timedelta

from django import forms
from django.contrib.auth import password_validation
from django.utils import timezone

from accounts.models import PermissionGrant, Principal
from products.models import Product
from releasegate.models import ChannelAccount


DATETIME_INPUT_FORMAT = "%Y-%m-%dT%H:%M"

SCOPE_CHOICES_ZH = (
    (PermissionGrant.ScopeKind.GLOBAL, "全部业务"),
    (PermissionGrant.ScopeKind.PRODUCT, "一个产品"),
    (PermissionGrant.ScopeKind.PLATFORM, "一个平台"),
    (PermissionGrant.ScopeKind.ACCOUNT, "一个发布账号"),
    (PermissionGrant.ScopeKind.SURFACE, "一个页面或功能"),
)
ACTION_CHOICES_ZH = (
    (PermissionGrant.Action.VIEW, "查看"),
    (PermissionGrant.Action.EDIT, "编辑"),
    (PermissionGrant.Action.CREATE_TASK, "创建任务"),
    (PermissionGrant.Action.ASSIGN_TASK, "分配任务"),
    (PermissionGrant.Action.CANCEL_TASK, "取消任务"),
    (PermissionGrant.Action.COMPLETE_TASK, "完成任务"),
    (PermissionGrant.Action.REVIEW, "审核"),
    (PermissionGrant.Action.APPROVE, "批准"),
    (PermissionGrant.Action.PUBLISH, "发布"),
    (PermissionGrant.Action.MANAGE_ACCOUNT, "管理员工和账号"),
    (PermissionGrant.Action.EMERGENCY_STOP, "紧急停止"),
    (PermissionGrant.Action.COLLECT_READ_ONLY, "只读采集"),
)
EFFECT_CHOICES_ZH = (
    (PermissionGrant.Effect.ALLOW, "允许"),
    (PermissionGrant.Effect.DENY, "明确拒绝（优先于允许）"),
)
RISK_CHOICES_ZH = (
    (PermissionGrant.RiskLevel.LOW, "低"),
    (PermissionGrant.RiskLevel.MEDIUM, "中"),
    (PermissionGrant.RiskLevel.HIGH, "高"),
    (PermissionGrant.RiskLevel.CRITICAL, "严重"),
)


class GrantIssueForm(forms.Form):
    scope_kind = forms.ChoiceField(label="权限范围", choices=SCOPE_CHOICES_ZH)
    product = forms.ModelChoiceField(
        label="产品",
        queryset=Product.objects.none(),
        required=False,
        empty_label="请选择产品",
    )
    platform_code = forms.CharField(label="平台代码", max_length=64, required=False)
    channel_account = forms.ModelChoiceField(
        label="发布账号",
        queryset=ChannelAccount.objects.none(),
        required=False,
        empty_label="请选择一个账号",
    )
    surface_ref = forms.CharField(label="页面或功能标识", max_length=255, required=False)
    action = forms.ChoiceField(label="允许做什么", choices=ACTION_CHOICES_ZH)
    effect = forms.ChoiceField(label="允许或拒绝", choices=EFFECT_CHOICES_ZH)
    risk_level = forms.ChoiceField(label="风险等级", choices=RISK_CHOICES_ZH)
    valid_from = forms.DateTimeField(
        label="开始时间",
        input_formats=[DATETIME_INPUT_FORMAT],
        widget=forms.DateTimeInput(format=DATETIME_INPUT_FORMAT, attrs={"type": "datetime-local"}),
    )
    valid_until = forms.DateTimeField(
        label="到期时间",
        required=False,
        input_formats=[DATETIME_INPUT_FORMAT],
        widget=forms.DateTimeInput(format=DATETIME_INPUT_FORMAT, attrs={"type": "datetime-local"}),
    )

    def __init__(self, *args, actor: Principal, **kwargs):
        super().__init__(*args, **kwargs)
        self.actor = actor
        self.fields["product"].queryset = Product.objects.order_by("name", "product_code")
        self.fields["channel_account"].queryset = ChannelAccount.objects.filter(
            status=ChannelAccount.Status.ACTIVE
        ).order_by("platform_code", "display_name")
        if actor.role == Principal.Role.OPERATIONS_ADMIN:
            forbidden = {
                PermissionGrant.Action.PUBLISH,
                PermissionGrant.Action.MANAGE_ACCOUNT,
                PermissionGrant.Action.EMERGENCY_STOP,
            }
            self.fields["action"].choices = [
                choice for choice in ACTION_CHOICES_ZH if choice[0] not in forbidden
            ]
        now = timezone.localtime().replace(second=0, microsecond=0)
        self.initial.setdefault("valid_from", now)
        self.initial.setdefault("valid_until", now + timedelta(days=30))
        self.initial.setdefault("effect", PermissionGrant.Effect.ALLOW)
        self.initial.setdefault("risk_level", PermissionGrant.RiskLevel.LOW)

    def clean(self):
        cleaned = super().clean()
        scope_kind = cleaned.get("scope_kind")
        product = cleaned.get("product")
        platform_code = (cleaned.get("platform_code") or "").strip()
        channel_account = cleaned.get("channel_account")
        surface_ref = (cleaned.get("surface_ref") or "").strip()
        supplied = {
            PermissionGrant.ScopeKind.PRODUCT: bool(product),
            PermissionGrant.ScopeKind.PLATFORM: bool(platform_code),
            PermissionGrant.ScopeKind.ACCOUNT: bool(channel_account),
            PermissionGrant.ScopeKind.SURFACE: bool(surface_ref),
        }
        if scope_kind == PermissionGrant.ScopeKind.GLOBAL:
            if any(supplied.values()):
                raise forms.ValidationError("全局权限不能同时填写产品、平台、账号或页面范围。")
        elif not supplied.get(scope_kind, False):
            raise forms.ValidationError("请填写与所选权限范围对应的具体对象。")
        elif sum(supplied.values()) != 1:
            raise forms.ValidationError("每条权限只能绑定一个明确范围。")

        valid_from = cleaned.get("valid_from")
        valid_until = cleaned.get("valid_until")
        if valid_from and valid_until and valid_until <= valid_from:
            self.add_error("valid_until", "到期时间必须晚于开始时间。")
        if cleaned.get("action") == PermissionGrant.Action.PUBLISH:
            if scope_kind != PermissionGrant.ScopeKind.ACCOUNT or not channel_account:
                self.add_error("channel_account", "发布权限必须明确绑定一个发布账号。")
            if cleaned.get("risk_level") not in {
                PermissionGrant.RiskLevel.HIGH,
                PermissionGrant.RiskLevel.CRITICAL,
            }:
                self.add_error("risk_level", "发布权限必须标记为高风险或严重风险。")
            if valid_until is None:
                self.add_error("valid_until", "发布权限必须设置到期时间。")
        return cleaned


class GrantRenewForm(forms.Form):
    valid_from = forms.DateTimeField(
        label="新权限开始时间",
        input_formats=[DATETIME_INPUT_FORMAT],
        widget=forms.DateTimeInput(format=DATETIME_INPUT_FORMAT, attrs={"type": "datetime-local"}),
    )
    valid_until = forms.DateTimeField(
        label="新权限到期时间",
        required=False,
        input_formats=[DATETIME_INPUT_FORMAT],
        widget=forms.DateTimeInput(format=DATETIME_INPUT_FORMAT, attrs={"type": "datetime-local"}),
    )

    def __init__(self, *args, grant: PermissionGrant, **kwargs):
        super().__init__(*args, **kwargs)
        self.grant = grant
        now = timezone.localtime().replace(second=0, microsecond=0)
        self.initial.setdefault("valid_from", max(now, timezone.localtime(grant.valid_from)))
        self.initial.setdefault("valid_until", now + timedelta(days=30))

    def clean(self):
        cleaned = super().clean()
        valid_from = cleaned.get("valid_from")
        valid_until = cleaned.get("valid_until")
        if valid_from and valid_from < self.grant.valid_from:
            self.add_error("valid_from", "新权限不能早于原权限开始时间。")
        if valid_from and valid_until and valid_until <= valid_from:
            self.add_error("valid_until", "到期时间必须晚于开始时间。")
        if self.grant.action == PermissionGrant.Action.PUBLISH and valid_until is None:
            self.add_error("valid_until", "发布权限必须设置到期时间。")
        return cleaned


class GrantRevokeForm(forms.Form):
    reason = forms.CharField(
        label="撤销原因",
        max_length=500,
        widget=forms.Textarea(attrs={"rows": 3, "placeholder": "请说明为什么收回这项权限"}),
    )

    def clean_reason(self):
        reason = self.cleaned_data["reason"].strip()
        if not reason:
            raise forms.ValidationError("必须填写撤销原因。")
        return reason


class SelfPasswordChangeForm(forms.Form):
    current_password = forms.CharField(label="当前密码", strip=False, widget=forms.PasswordInput)
    new_password1 = forms.CharField(label="新密码", strip=False, widget=forms.PasswordInput)
    new_password2 = forms.CharField(label="再次输入新密码", strip=False, widget=forms.PasswordInput)

    def __init__(self, *args, user: Principal, **kwargs):
        super().__init__(*args, **kwargs)
        self.user = user

    def clean_current_password(self):
        password = self.cleaned_data["current_password"]
        if not self.user.check_password(password):
            raise forms.ValidationError("当前密码不正确。")
        return password

    def clean(self):
        cleaned = super().clean()
        password1 = cleaned.get("new_password1")
        password2 = cleaned.get("new_password2")
        if password1 and password2 and password1 != password2:
            self.add_error("new_password2", "两次输入的新密码不一致。")
        if password1:
            try:
                password_validation.validate_password(password1, self.user)
            except forms.ValidationError as error:
                self.add_error("new_password1", error)
        return cleaned

    def save(self):
        self.user.set_password(self.cleaned_data["new_password1"])
        self.user.must_change_password = False
        self.user.save(update_fields=["password", "must_change_password"])
        return self.user
