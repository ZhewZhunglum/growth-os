from __future__ import annotations

import json
import uuid
from types import SimpleNamespace

from django.test import SimpleTestCase

from dailyops.content_generation import _evidence_manifest, _request
from insights.models import (
    CommerceObservation,
    DataDomain,
    GEOProbeResult,
    PublicationPerformanceObservation,
)
from integrations.connectors.types import Platform


class ContentGenerationDataDomainBoundaryTests(SimpleTestCase):
    def test_internal_observations_never_enter_generation_request_or_evidence_manifest(self):
        external_evidence = SimpleNamespace(
            pk=uuid.uuid4(),
            title="External demand question",
            excerpt="People are asking for a simpler afternoon routine.",
            facts={"signal": "external-demand-only"},
            external_url="https://www.pinterest.com/pin/external-demand-only/",
            provenance_sha256="a" * 64,
            source_id=uuid.uuid4(),
        )

        # These are real internal-domain model types. They are deliberately
        # attached to the fake context as traps: request/manifest construction
        # must use only the explicit ExternalEvidenceItem list.
        performance = PublicationPerformanceObservation(pk=uuid.uuid4())
        commerce = CommerceObservation(pk=uuid.uuid4())
        geo = GEOProbeResult(pk=uuid.uuid4())
        # PROCESS_TELEMETRY is a reserved data domain in V1, but the current
        # codebase intentionally has no ProcessTelemetryObservation entity.
        # Keep a domain/id sentinel here rather than inventing a fake model.
        process_telemetry = SimpleNamespace(
            pk=uuid.uuid4(),
            data_domain=DataDomain.PROCESS_TELEMETRY,
        )

        profile = SimpleNamespace(
            pk=uuid.uuid4(),
            language_code="en",
            audience={"primary": "US consumers"},
            core_value_proposition="A measured product angle.",
            brand_voice={"tone": ["clear"]},
            product_facts={"format": "capsule"},
            prohibited_expressions=["cure"],
        )
        context = SimpleNamespace(
            pk=uuid.uuid4(),
            task_id=uuid.uuid4(),
            task=SimpleNamespace(title="Create one TikTok script", description="Answer external demand."),
            product_profile_version=profile,
            claim_matrix_version_id=uuid.uuid4(),
            policy_set_snapshot=[{"id": str(uuid.uuid4()), "manifest_sha256": "b" * 64}],
            input_payload_sha256="c" * 64,
            publication_performance_observations=[performance],
            commerce_observations=[commerce],
            geo_probe_results=[geo],
            process_telemetry_observations=[process_telemetry],
        )

        request = _request(
            context=context,
            platform=Platform.TIKTOK,
            evidence=[external_evidence],
            command_id=uuid.uuid4(),
        )
        payload = json.loads(request.messages[1].content)
        manifest = _evidence_manifest([external_evidence])
        serialized = json.dumps(
            {"request": payload, "manifest": manifest},
            sort_keys=True,
        )

        self.assertEqual(
            payload["evidence"],
            [
                {
                    "id": str(external_evidence.pk),
                    "title": external_evidence.title,
                    "excerpt": external_evidence.excerpt,
                    "facts": external_evidence.facts,
                    "external_url": external_evidence.external_url,
                }
            ],
        )
        self.assertEqual(
            manifest,
            [
                {
                    "id": str(external_evidence.pk),
                    "provenance_sha256": external_evidence.provenance_sha256,
                    "source_id": str(external_evidence.source_id),
                }
            ],
        )
        for forbidden_id in (
            performance.pk,
            commerce.pk,
            geo.pk,
            process_telemetry.pk,
        ):
            self.assertNotIn(str(forbidden_id), serialized)
        for forbidden_domain in (
            DataDomain.CONTENT_PERFORMANCE,
            DataDomain.COMMERCE_OUTCOME,
            DataDomain.GEO,
            DataDomain.PROCESS_TELEMETRY,
        ):
            self.assertNotIn(forbidden_domain, serialized)
