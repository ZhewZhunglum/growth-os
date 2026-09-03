from datetime import timedelta

from django.test import TestCase
from django.utils import timezone

from accounts.models import Principal
from dailyops.forms import (
    BatchDispositionForm,
    ChannelPlanForm,
    EvidenceCorrectionForm,
    ManualEvidenceForm,
    TransitionForm,
    platform_label,
)
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

    def test_channel_plan_excludes_analytical_and_already_planned_platforms(self):
        pinterest = ChannelAccount.objects.create(
            platform_code=Platform.PINTEREST.value,
            account_code="puko-pinterest-friendly",
            external_account_ref="puko-pinterest-friendly",
            display_name="PUKO Pinterest",
            created_by_principal=self.owner,
            updated_by_principal=self.owner,
        )
        gsc = ChannelAccount.objects.create(
            platform_code=Platform.GOOGLE_SEARCH_CONSOLE.value,
            account_code="puko-gsc-analysis",
            external_account_ref="puko-gsc-analysis",
            display_name="PUKO GSC analysis",
            created_by_principal=self.owner,
            updated_by_principal=self.owner,
        )

        form = ChannelPlanForm(
            accounts=ChannelAccount.objects.filter(pk__in=[self.account.pk, pinterest.pk, gsc.pk]),
            excluded_platforms=[Platform.TIKTOK],
        )

        self.assertEqual(form.fields["platform"].choices, [("PINTEREST", "Pinterest")])
        self.assertTrue(form.has_platform_choices)
        self.assertNotIn(gsc.pk, form.fields["channel_account"].queryset.values_list("pk", flat=True))

    def test_channel_plan_reports_when_no_execution_platform_remains(self):
        form = ChannelPlanForm(
            accounts=ChannelAccount.objects.filter(pk=self.account.pk),
            excluded_platforms=[Platform.TIKTOK],
        )

        self.assertFalse(form.has_platform_choices)
        self.assertEqual(form.fields["platform"].choices, [])

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

    def test_ordinary_note_forms_leave_source_tagging_to_the_service_boundary(self):
        blank_batch = BatchDispositionForm(
            data={
                "command_id": "3f069a44-5bc5-49c2-baf8-97ae2309e803",
                "reason": "",
                "confirm": "on",
            }
        )
        blank_transition = TransitionForm(
            data={
                "command_id": "3f069a44-5bc5-49c2-baf8-97ae2309e804",
                "expected_version": 0,
                "to_state": "APPROVED",
                "reason": "",
            }
        )
        user_correction = EvidenceCorrectionForm(
            data={
                "command_id": "3f069a44-5bc5-49c2-baf8-97ae2309e805",
                "platform": Platform.TIKTOK.value,
                "reference": "https://www.tiktok.com/@puko/video/123",
                "collected_at": timezone.now().isoformat(),
                "title": "Corrected evidence",
                "reason": "原链接贴错了。",
            }
        )

        self.assertTrue(blank_batch.is_valid(), blank_batch.errors)
        self.assertTrue(blank_transition.is_valid(), blank_transition.errors)
        self.assertTrue(user_correction.is_valid(), user_correction.errors)
        self.assertEqual(blank_batch.cleaned_data["reason"], "")
        self.assertEqual(blank_transition.cleaned_data["reason"], "")
        self.assertEqual(user_correction.cleaned_data["reason"], "原链接贴错了。")

    def test_channel_plan_explains_exhausted_vs_unavailable_accounts(self):
        exhausted = ChannelPlanForm(
            accounts=ChannelAccount.objects.filter(pk=self.account.pk),
            excluded_platforms=[Platform.TIKTOK],
        )
        unavailable = ChannelPlanForm(accounts=ChannelAccount.objects.none())

        self.assertEqual(exhausted.no_platform_reason, "EXHAUSTED")
        self.assertEqual(unavailable.no_platform_reason, "UNAVAILABLE")
