from __future__ import annotations

import unittest
import uuid
from datetime import datetime, timedelta, timezone

from integrations.browser_worker import BrowserJobOperation, BrowserWorkerJob, BrowserWorkerPairing
from integrations.connectors import Platform
from integrations.errors import BrowserWorkerProtocolError


NOW = datetime(2026, 8, 21, 8, 0, tzinfo=timezone.utc)


class BrowserWorkerProtocolTests(unittest.TestCase):
    def pairing(self, *, dedicated: bool = True) -> BrowserWorkerPairing:
        return BrowserWorkerPairing(
            pairing_id=uuid.uuid4(),
            worker_id="worker-1",
            dedicated_profile_id="profile-1",
            dedicated_profile_label="Growth OS TikTok",
            browser_family="chromium",
            paired_at=NOW,
            expires_at=NOW + timedelta(days=30),
            capabilities=(Platform.TIKTOK.value,),
            dedicated_profile=dedicated,
        )

    def test_shared_browser_profile_is_rejected(self):
        with self.assertRaises(BrowserWorkerProtocolError):
            self.pairing(dedicated=False)

    def test_job_is_bound_to_exact_pairing_profile_and_allowed_hosts(self):
        pairing = self.pairing()
        job = BrowserWorkerJob(
            job_id=uuid.uuid4(),
            operation_key="daily:2026-08-21:tiktok",
            platform=Platform.TIKTOK,
            operation=BrowserJobOperation.SEARCH,
            pairing_id=pairing.pairing_id,
            dedicated_profile_id=pairing.dedicated_profile_id,
            created_at=NOW,
            expires_at=NOW + timedelta(minutes=15),
            query="focus supplement",
            max_items=20,
            allowed_hosts=("tiktok.com", "ads.tiktok.com"),
        )
        job.validate_pairing(pairing, NOW + timedelta(minutes=1))
        self.assertEqual(len(job.fingerprint), 64)

    def test_expired_job_is_rejected(self):
        pairing = self.pairing()
        job = BrowserWorkerJob(
            job_id=uuid.uuid4(),
            operation_key="daily:2026-08-21:tiktok",
            platform=Platform.TIKTOK,
            operation=BrowserJobOperation.COLLECT,
            pairing_id=pairing.pairing_id,
            dedicated_profile_id=pairing.dedicated_profile_id,
            created_at=NOW,
            expires_at=NOW + timedelta(minutes=1),
            query="focus supplement",
            max_items=20,
            allowed_hosts=("tiktok.com",),
        )
        with self.assertRaises(BrowserWorkerProtocolError):
            job.validate_pairing(pairing, NOW + timedelta(minutes=2))


if __name__ == "__main__":
    unittest.main()
