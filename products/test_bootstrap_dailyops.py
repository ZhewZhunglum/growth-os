from __future__ import annotations

from io import StringIO
from unittest.mock import patch

from django.core.management import call_command
from django.test import TestCase

from accounts.models import PermissionGrant, Principal
from insights.models import GEOProbePanel, GEOProbePanelItem
from intelligence.models import SourceRegistry
from products.models import Product
from releasegate.models import ChannelAccount
from workflow.models import Task, TaskContractVersion


class BootstrapDailyOperationsTests(TestCase):
    PASSWORDS = {
        "BOOTSTRAP_OWNER_PASSWORD": "LocalOwner!91",
        "BOOTSTRAP_ADMIN_PASSWORD": "LocalAdmin!82",
        "BOOTSTRAP_OPERATOR_PASSWORD": "LocalOperator!73",
    }

    def _run(self):
        output = StringIO()
        with patch.dict("os.environ", self.PASSWORDS, clear=False):
            call_command("bootstrap_dogfood", "--full-demo", stdout=output)
        return output.getvalue()

    def test_fresh_local_bootstrap_is_ready_for_offline_daily_operations(self):
        self._run()

        owner = Principal.objects.get(username="owner")
        admin = Principal.objects.get(username="admin")
        operator = Principal.objects.get(username="operator")
        for principal in (owner, admin, operator):
            self.assertFalse(principal.is_superuser)
            self.assertFalse(principal.is_staff)
            self.assertEqual(
                PermissionGrant.objects.filter(
                    principal=principal,
                    action=PermissionGrant.Action.COLLECT_READ_ONLY,
                    scope_kind=PermissionGrant.ScopeKind.PLATFORM,
                ).count(),
                7,
            )
        for principal in (owner, admin):
            self.assertTrue(
                PermissionGrant.objects.filter(
                    principal=principal,
                    action=PermissionGrant.Action.MANAGE_ACCOUNT,
                    scope_kind=PermissionGrant.ScopeKind.GLOBAL,
                ).exists()
            )
        for principal, expected_actions in (
            (
                owner,
                {
                    PermissionGrant.Action.VIEW,
                    PermissionGrant.Action.EDIT,
                    PermissionGrant.Action.APPROVE,
                    PermissionGrant.Action.MANAGE_ACCOUNT,
                },
            ),
            (
                admin,
                {
                    PermissionGrant.Action.VIEW,
                    PermissionGrant.Action.EDIT,
                    PermissionGrant.Action.APPROVE,
                    PermissionGrant.Action.MANAGE_ACCOUNT,
                },
            ),
            (
                operator,
                {
                    PermissionGrant.Action.VIEW,
                    PermissionGrant.Action.EDIT,
                },
            ),
        ):
            self.assertSetEqual(
                set(
                    PermissionGrant.objects.filter(
                        principal=principal,
                        scope_kind=PermissionGrant.ScopeKind.GLOBAL,
                    ).values_list("action", flat=True)
                ),
                expected_actions,
            )

        product = Product.objects.get(product_code="PUKO")
        channel = ChannelAccount.objects.get(account_code="puko-us")
        for principal in (owner, admin, operator):
            publish_grant = PermissionGrant.objects.get(
                principal=principal,
                action=PermissionGrant.Action.PUBLISH,
                scope_kind=PermissionGrant.ScopeKind.ACCOUNT,
                account_ref=channel.account_code,
            )
            self.assertEqual(publish_grant.effect, PermissionGrant.Effect.ALLOW)
            self.assertEqual(publish_grant.risk_level, PermissionGrant.RiskLevel.HIGH)
            self.assertIsNotNone(publish_grant.valid_until)
        profile = product.current_profile_version
        self.assertIsNotNone(profile)
        self.assertTrue(profile.is_sealed)
        self.assertTrue(profile.objective_profile_version.is_sealed)
        self.assertTrue(profile.claim_matrix_version.is_sealed)
        self.assertTrue(profile.evidence_library_version.is_sealed)
        self.assertTrue(
            TaskContractVersion.objects.filter(
                product_profile_version=profile,
                sealed_at__isnull=False,
            ).exists()
        )
        self.assertEqual(SourceRegistry.objects.filter(source_key__startswith="daily-").count(), 28)
        geo_panel = GEOProbePanel.objects.get(panel_key="local-test-puko-geo", version_number=1)
        self.assertEqual(geo_panel.product, product)
        geo_item = GEOProbePanelItem.objects.get(panel=geo_panel, item_number=1)
        self.assertIn("[LOCAL TEST ONLY]", geo_item.question)
        self.assertEqual(geo_item.intent, "LOCAL_DOGFOOD_GEO_VISIBILITY")
        self.assertFalse(Task.objects.exists())

    def test_replay_does_not_duplicate_context_sources_or_grants(self):
        self._run()
        counts = {
            "principals": Principal.objects.count(),
            "grants": PermissionGrant.objects.count(),
            "sources": SourceRegistry.objects.count(),
            "contracts": TaskContractVersion.objects.count(),
            "geo_panels": GEOProbePanel.objects.count(),
            "geo_items": GEOProbePanelItem.objects.count(),
        }

        self._run()

        self.assertEqual(Principal.objects.count(), counts["principals"])
        self.assertEqual(PermissionGrant.objects.count(), counts["grants"])
        self.assertEqual(SourceRegistry.objects.count(), counts["sources"])
        self.assertEqual(TaskContractVersion.objects.count(), counts["contracts"])
        self.assertEqual(GEOProbePanel.objects.count(), counts["geo_panels"])
        self.assertEqual(GEOProbePanelItem.objects.count(), counts["geo_items"])
