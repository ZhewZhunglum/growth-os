from __future__ import annotations

import unittest
from datetime import datetime, timezone

from integrations.connectors import AcquisitionMode, Platform
from integrations.errors import IngestionValidationError
from integrations.ingestion import CSVIngestionValidator, ManualEvidenceInput, validate_manual_evidence


class IngestionTests(unittest.TestCase):
    def test_csv_creates_link_only_evidence_with_provenance(self):
        content = (
            "external_id,url,title,content_text,collected_at,language_code,market_code,query\n"
            "pin-1,https://pinterest.com/pin/1,Focus ideas,Useful text,2026-08-21T08:00:00Z,en,US,focus\n"
        )
        result = CSVIngestionValidator().validate(
            content,
            platform=Platform.PINTEREST,
            source_key="pinterest.csv",
            collection_run_key="daily.2026-08-21.pinterest",
            collected_by="principal_1",
        )
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].provenance.acquisition_mode, AcquisitionMode.CSV)
        self.assertEqual(result[0].provenance.row_number, 2)
        self.assertTrue(result[0].dedupe_key.startswith("dk1_"))

    def test_manual_link_is_normalized_and_versionable(self):
        value = ManualEvidenceInput(
            platform=Platform.QUORA,
            source_key="quora.manual",
            collection_run_key="daily.2026-08-21.quora",
            collected_by="principal_1",
            collected_at=datetime(2026, 8, 21, 8, 0, tzinfo=timezone.utc),
            url="HTTPS://Quora.COM/example",
            title="Question",
        )
        result = validate_manual_evidence(value)
        self.assertEqual(result.url, "https://quora.com/example")
        self.assertEqual(result.provenance.acquisition_mode, AcquisitionMode.MANUAL)

    def test_csv_rejects_rows_without_timezone(self):
        content = "url,title,collected_at\nhttps://example.com,Title,2026-08-21T08:00:00\n"
        with self.assertRaises(IngestionValidationError):
            CSVIngestionValidator().validate(
                content,
                platform=Platform.GOOGLE_SEARCH,
                source_key="google.csv",
                collection_run_key="daily.2026-08-21.google",
                collected_by="principal_1",
            )

    def test_manual_input_rejects_url_credentials(self):
        value = ManualEvidenceInput(
            platform=Platform.SHOPIFY,
            source_key="shopify.manual",
            collection_run_key="daily.2026-08-21.shopify",
            collected_by="principal_1",
            collected_at=datetime.now(timezone.utc),
            url="https://user:password@example.com/data",
            title="Bad URL",
        )
        with self.assertRaises(IngestionValidationError):
            validate_manual_evidence(value)

    def test_csv_rejects_extra_values_not_declared_in_header(self):
        content = (
            "url,title,collected_at\n"
            "https://example.com,Title,2026-08-21T08:00:00Z,unexpected\n"
        )
        with self.assertRaises(IngestionValidationError):
            CSVIngestionValidator().validate(
                content,
                platform=Platform.GOOGLE_SEARCH,
                source_key="google.csv",
                collection_run_key="daily.2026-08-21.google",
                collected_by="principal_1",
            )

    def test_same_manual_payload_has_stable_dedupe_key(self):
        value = ManualEvidenceInput(
            platform=Platform.GOOGLE_SEARCH,
            source_key="google.manual",
            collection_run_key="daily.2026-08-21.google",
            collected_by="principal_1",
            collected_at=datetime(2026, 8, 21, 8, 0, tzinfo=timezone.utc),
            external_id="search-result-1",
            title="Result",
        )
        self.assertEqual(validate_manual_evidence(value).dedupe_key, validate_manual_evidence(value).dedupe_key)


if __name__ == "__main__":
    unittest.main()
