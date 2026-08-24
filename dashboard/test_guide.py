from django.test import TestCase
from django.urls import reverse

from accounts.models import Principal


class GuidePageTests(TestCase):
    def setUp(self):
        self.user = Principal.objects.create_user(
            username="guide-owner",
            password="safe-local-password-123",
            role=Principal.Role.OWNER,
        )
        self.client.force_login(self.user)

    def test_chinese_guide_explains_daily_work_in_plain_language(self):
        response = self.client.get(reverse("dashboard:guide"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "一次 Daily Task 到底怎么跑")
        self.assertContains(response, "AI 搜索曝光")
        self.assertNotContains(response, "先弄清楚为什么做，再开始做")

    def test_english_guide_switches_the_whole_page_copy(self):
        self.client.post(
            reverse("set_language"),
            {"language": "en", "next": reverse("dashboard:guide")},
        )
        response = self.client.get(reverse("dashboard:guide"))
        self.assertContains(response, "How does one Daily Task run")
        self.assertContains(response, "AI search visibility")
