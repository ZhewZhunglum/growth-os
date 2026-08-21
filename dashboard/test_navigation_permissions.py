from datetime import timedelta

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from accounts.models import PermissionGrant, Principal
from products.models import Product
from releasegate.models import ChannelAccount


class PermissionAwareNavigationTests(TestCase):
    def setUp(self):
        self.user = Principal.objects.create_user(
            username="navigation-user",
            password="LocalPassword123!",
            display_name="Navigation User",
            role=Principal.Role.OPERATIONS_ADMIN,
        )
        self.product = Product.objects.create(
            product_code="friendly-navigation-product",
            name="PUKO Focus",
            market_code="US",
            language_code="en",
            created_by_principal=self.user,
            updated_by_principal=self.user,
        )
        self.account = ChannelAccount.objects.create(
            platform_code="TIKTOK",
            account_code="friendly-navigation-tiktok",
            external_account_ref="friendly-navigation-tiktok",
            display_name="PUKO TikTok",
            created_by_principal=self.user,
            updated_by_principal=self.user,
        )
        self.client.force_login(self.user)

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

    def test_review_and_publish_links_follow_live_grants_not_role(self):
        review_url = reverse("dashboard:review-queue")
        release_url = reverse("dashboard:release-queue")

        without_grants = self.client.get(reverse("dashboard:home"))
        self.assertNotContains(without_grants, f'href="{review_url}"')
        self.assertNotContains(without_grants, f'href="{release_url}"')

        self.grant(
            action=PermissionGrant.Action.REVIEW,
            scope_kind=PermissionGrant.ScopeKind.PRODUCT,
            product=self.product,
        )
        with_review = self.client.get(reverse("dashboard:home"))
        self.assertContains(with_review, f'href="{review_url}"')
        self.assertNotContains(with_review, f'href="{release_url}"')

        self.grant(
            action=PermissionGrant.Action.PUBLISH,
            scope_kind=PermissionGrant.ScopeKind.ACCOUNT,
            account_ref=self.account.account_code,
            risk=PermissionGrant.RiskLevel.HIGH,
        )
        with_publish = self.client.get(reverse("dashboard:home"))
        self.assertContains(with_publish, f'href="{review_url}"')
        self.assertContains(with_publish, f'href="{release_url}"')
        self.assertContains(with_publish, "更多")
