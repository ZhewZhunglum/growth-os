from types import SimpleNamespace

from django.test import SimpleTestCase

from feedbackui.forms import (
    channel_account_choice_label,
    geo_item_choice_label,
    geo_panel_choice_label,
    publication_choice_label,
)


class FeedbackFriendlyChoiceLabelTests(SimpleTestCase):
    def test_visible_choice_labels_describe_objects_without_uuid_noise(self):
        account = SimpleNamespace(
            display_name="PUKO Pinterest",
            account_code="puko-pinterest",
            platform_code="PINTEREST",
        )
        account_label = channel_account_choice_label(account)
        self.assertEqual(account_label, "PUKO Pinterest · Pinterest")

        task = SimpleNamespace(title="Publish focus guide")
        publication = SimpleNamespace(
            submission=SimpleNamespace(task=task),
            current_gate=SimpleNamespace(channel_account=account),
            created_at=None,
        )
        self.assertEqual(
            publication_choice_label(publication),
            "Publish focus guide · PUKO Pinterest",
        )
        product = SimpleNamespace(name="PUKO Focus")
        panel = SimpleNamespace(
            product=product,
            panel_key="puko-focus-us",
            version_number=2,
            market_code="US",
            language_code="en",
        )
        self.assertEqual(geo_panel_choice_label(panel), "PUKO Focus · US/en · 第 2 版")

        item = SimpleNamespace(
            panel=panel,
            item_number=3,
            question="What helps with afternoon focus?",
        )
        self.assertEqual(
            geo_item_choice_label(item),
            "正式问题 · PUKO Focus · 问题 3 · What helps with afternoon focus?",
        )

    def test_local_seed_choice_is_explicit_even_when_question_is_truncated(self):
        panel = SimpleNamespace(
            product=SimpleNamespace(name="PUKO"),
            panel_key="local-test-puko-geo",
        )
        item = SimpleNamespace(
            panel=panel,
            item_number=1,
            intent="LOCAL_DOGFOOD_GEO_VISIBILITY",
            question="How does a very long system-preset question appear in the compact selector without losing its source label?",
        )

        label = geo_item_choice_label(item)

        self.assertTrue(label.startswith("本地测试题 · 系统预设 · PUKO · 问题 1"))
        self.assertTrue(label.endswith("…"))
