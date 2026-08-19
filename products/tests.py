from django.core.exceptions import ValidationError
from django.test import TestCase

from accounts.models import Principal
from products.models import Product, ProductProfileVersion


class ProductProfileVersionTests(TestCase):
    def setUp(self):
        self.owner = Principal.objects.create_user(username="owner", password="not-a-real-secret")
        self.product = Product.objects.create(
            product_code="PUKO", name="PUKO Nutrition", market_code="US", language_code="en",
            created_by_principal=self.owner, updated_by_principal=self.owner,
        )
        self.profile = ProductProfileVersion.objects.create(
            product=self.product, version_number=1, market_code="US", language_code="en",
            audience={"primary": "US wellness consumers"},
            core_value_proposition="Evidence-informed daily wellness products.",
            brand_voice={"tone": ["clear", "measured", "evidence-informed"]},
            product_facts={"business_mode": "B2C"},
            prohibited_expressions=["cure", "treat", "prevent disease"],
            created_by_principal=self.owner,
        )

    def test_seal_records_manifest_and_blocks_edits(self):
        self.profile.seal(self.owner)
        self.assertTrue(self.profile.is_sealed)
        self.assertEqual(len(self.profile.manifest_sha256), 64)
        self.profile.core_value_proposition = "Mutated after sealing"
        with self.assertRaises(ValidationError):
            self.profile.save()

    def test_product_current_profile_must_belong_to_product(self):
        other = Product.objects.create(
            product_code="OTHER", name="Other", market_code="US", language_code="en",
            created_by_principal=self.owner, updated_by_principal=self.owner,
        )
        self.profile.seal(self.owner)
        other.current_profile_version = self.profile
        with self.assertRaises(ValidationError):
            other.full_clean()

