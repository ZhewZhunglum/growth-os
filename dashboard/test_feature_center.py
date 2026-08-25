from datetime import timedelta

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from accounts.models import PermissionGrant, Principal
from products.models import Product
from releasegate.models import ChannelAccount


class FeatureCenterTests(TestCase):
    def setUp(self):
        self.user = Principal.objects.create_user(
            username="feature-center-admin",
            password="LocalPassword123!",
            display_name="Feature Center Admin",
            role=Principal.Role.OPERATIONS_ADMIN,
        )
        self.product = Product.objects.create(
            product_code="feature-center-product",
            name="PUKO Focus",
            market_code="US",
            language_code="en",
            created_by_principal=self.user,
            updated_by_principal=self.user,
        )
        self.account = ChannelAccount.objects.create(
            platform_code="TIKTOK",
            account_code="feature-center-tiktok",
            external_account_ref="feature-center-tiktok",
            display_name="PUKO TikTok",
            created_by_principal=self.user,
            updated_by_principal=self.user,
        )

    def grant(self, *, action, scope_kind, product=None, account_ref="", risk="LOW"):
        return PermissionGrant.objects.create(
            principal=self.user,
            scope_kind=scope_kind,
            product=product,
            account_ref=account_ref,
            action=action,
            effect=PermissionGrant.Effect.ALLOW,
            risk_level=risk,
            valid_from=timezone.now() - timedelta(minutes=1),
            valid_until=timezone.now() + timedelta(days=1),
            granted_by_principal=self.user,
        )

    def test_route_requires_login(self):
        feature_url = reverse("dashboard:feature-center")
        response = self.client.get(feature_url)
        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse("login"), response.url)

        self.client.force_login(self.user)
        response = self.client.get(feature_url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "功能中心")
        self.assertContains(response, "执行我的任务")
        self.assertContains(response, "使用说明")

    def test_features_follow_live_grants_instead_of_role(self):
        self.client.force_login(self.user)
        feature_url = reverse("dashboard:feature-center")

        without_grants = self.client.get(feature_url)
        self.assertNotContains(without_grants, "找机会并安排工作")
        self.assertNotContains(without_grants, "AI 搜索曝光（GEO）")
        self.assertNotContains(without_grants, "问题与规则治理")
        self.assertNotContains(without_grants, "员工与权限")

        self.grant(
            action=PermissionGrant.Action.VIEW,
            scope_kind=PermissionGrant.ScopeKind.PRODUCT,
            product=self.product,
        )
        with_product_view = self.client.get(feature_url)
        self.assertContains(with_product_view, "找机会并安排工作")

        self.grant(
            action=PermissionGrant.Action.COLLECT_READ_ONLY,
            scope_kind=PermissionGrant.ScopeKind.PRODUCT,
            product=self.product,
        )
        with_geo = self.client.get(feature_url)
        self.assertContains(with_geo, "AI 搜索曝光（GEO）")
        self.assertContains(with_geo, f'{reverse("feedback:home")}#geo')

        self.grant(
            action=PermissionGrant.Action.REVIEW,
            scope_kind=PermissionGrant.ScopeKind.PRODUCT,
            product=self.product,
        )
        self.grant(
            action=PermissionGrant.Action.PUBLISH,
            scope_kind=PermissionGrant.ScopeKind.ACCOUNT,
            account_ref=self.account.account_code,
            risk=PermissionGrant.RiskLevel.HIGH,
        )
        with_execution = self.client.get(feature_url)
        self.assertContains(with_execution, "审核别人提交的内容")
        self.assertContains(with_execution, "发布与确认完成")

        self.grant(
            action=PermissionGrant.Action.VIEW,
            scope_kind=PermissionGrant.ScopeKind.GLOBAL,
        )
        self.grant(
            action=PermissionGrant.Action.MANAGE_ACCOUNT,
            scope_kind=PermissionGrant.ScopeKind.GLOBAL,
            risk=PermissionGrant.RiskLevel.HIGH,
        )
        with_management = self.client.get(feature_url)
        self.assertContains(with_management, "问题与规则治理")
        self.assertContains(with_management, "员工与权限")
        self.assertContains(with_management, "产品、账号与运行设置")

    def test_geo_and_feature_center_labels_switch_to_english(self):
        self.grant(
            action=PermissionGrant.Action.COLLECT_READ_ONLY,
            scope_kind=PermissionGrant.ScopeKind.PRODUCT,
            product=self.product,
        )
        self.client.force_login(self.user)
        feature_url = reverse("dashboard:feature-center")
        self.client.post(reverse("set_language"), {"language": "en", "next": feature_url})

        response = self.client.get(feature_url)
        self.assertContains(response, "Feature center")
        self.assertContains(response, "AI search visibility (GEO)")
