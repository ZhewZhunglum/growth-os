from datetime import timedelta

from django.test import TestCase
from django.utils import timezone

from accounts.models import Principal
from dailyops.forms import ChannelPlanForm, ManualEvidenceForm, platform_label
from integrations.connectors.types import Platform
from releasegate.models import ChannelAccount


class DailyOperationsFriendlyFormTests(TestCase):
    def setUp(self):
        self.owner = Principal.objects.create_user(
            username="friendly-form-owner",
            password="LocalPassword123!",
            display_name="Friendly Owner",
            role=Principal.Role.OWNER,
        )
        self.account = ChannelAccount.objects.create(
            platform_code=Platform.TIKTOK.value,
            account_code="puko-tiktok-friendly",
            external_account_ref="puko-tiktok-friendly",
            display_name="PUKO TikTok",
            created_by_principal=self.owner,
            updated_by_principal=self.owner,
        )

    def test_channel_plan_uses_friendly_platform_and_account_labels(self):
        form = ChannelPlanForm(accounts=ChannelAccount.objects.filter(pk=self.account.pk))

        self.assertEqual(form.fields["platform"].choices, [("TIKTOK", "TikTok")])
        account_label = form.fields["channel_account"].label_from_instance(self.account)
        self.assertEqual(account_label, "PUKO TikTok · TikTok")
        self.assertNotIn(str(self.account.pk), account_label)

    def test_plan_date_renders_browser_safe_iso_value(self):
        form = ChannelPlanForm(
            accounts=ChannelAccount.objects.filter(pk=self.account.pk),
            initial={"plan_date": timezone.localdate()},
        )

        rendered = str(form["plan_date"])
        self.assertIn('type="date"', rendered)
        self.assertIn(f'value="{timezone.localdate():%Y-%m-%d}"', rendered)
        self.assertNotIn(f'{timezone.localdate():%Y/%m/%d}', rendered)

    def test_manual_platform_conflict_uses_friendly_names_not_enums(self):
        form = ManualEvidenceForm(
            data={
                "command_id": "3f069a44-5bc5-49c2-baf8-97ae2309e802",
                "platform": Platform.PINTEREST.value,
                "reference": "https://www.tiktok.com/@puko/video/123",
                "collected_at": timezone.now().isoformat(),
                "title": "Example",
            }
        )

        self.assertFalse(form.is_valid())
        error = " ".join(form.non_field_errors())
        self.assertIn("TikTok", error)
        self.assertIn("Pinterest", error)
        self.assertNotIn("TIKTOK", error)
        self.assertNotIn("PINTEREST", error)

    def test_platform_labels_are_bilingual_without_changing_values(self):
        self.assertEqual(platform_label(Platform.SHOPIFY), "Shopify / 独立站")
        self.assertEqual(platform_label(Platform.SHOPIFY, english=True), "Shopify store")
