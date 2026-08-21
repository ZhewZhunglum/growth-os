from __future__ import annotations

import csv
import hashlib
import io
import json
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime
from types import MappingProxyType
from typing import Any, Mapping
from urllib.parse import urlsplit, urlunsplit

from integrations.connectors.types import AcquisitionMode, Platform
from integrations.errors import IngestionValidationError


_SOURCE_KEY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,199}$")
_KNOWN_FIELDS = frozenset(
    {
        "external_id",
        "url",
        "title",
        "content_text",
        "collected_at",
        "language_code",
        "market_code",
        "query",
        "provider_version",
    }
)


@dataclass(frozen=True, slots=True)
class ProvenancePayload:
    platform: Platform
    source_key: str
    collection_run_key: str
    acquisition_mode: AcquisitionMode
    collected_by: str
    collected_at: datetime
    payload_digest: str
    external_id: str | None
    url: str | None
    row_number: int | None = None

    def __post_init__(self) -> None:
        if not _SOURCE_KEY.fullmatch(self.source_key):
            raise ValueError("source_key is invalid")
        if not _SOURCE_KEY.fullmatch(self.collection_run_key):
            raise ValueError("collection_run_key is invalid")
        if not self.collected_by or self.collected_at.tzinfo is None:
            raise ValueError("Provenance collector and timezone-aware collected_at are required")
        if not re.fullmatch(r"[0-9a-f]{64}", self.payload_digest):
            raise ValueError("payload_digest must be a full SHA-256 hex digest")
        if not self.external_id and not self.url:
            raise ValueError("Provenance requires an external_id or URL")


@dataclass(frozen=True, slots=True)
class IngestedEvidence:
    platform: Platform
    external_id: str | None
    url: str | None
    title: str
    content_text: str
    language_code: str
    market_code: str
    query: str
    attributes: Mapping[str, Any]
    provenance: ProvenancePayload
    dedupe_key: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "attributes", MappingProxyType(dict(self.attributes)))


@dataclass(frozen=True, slots=True)
class ManualEvidenceInput:
    platform: Platform
    source_key: str
    collection_run_key: str
    collected_by: str
    collected_at: datetime
    external_id: str | None = None
    url: str | None = None
    title: str = ""
    content_text: str = ""
    language_code: str = ""
    market_code: str = ""
    query: str = ""
    attributes: Mapping[str, Any] = field(default_factory=dict)


class CSVIngestionValidator:
    def __init__(self, *, max_bytes: int = 5_000_000, max_rows: int = 10_000):
        if max_bytes <= 0 or max_rows <= 0:
            raise ValueError("CSV limits must be positive")
        self.max_bytes = max_bytes
        self.max_rows = max_rows

    def validate(
        self,
        content: str | bytes,
        *,
        platform: Platform,
        source_key: str,
        collection_run_key: str,
        collected_by: str,
    ) -> tuple[IngestedEvidence, ...]:
        text = _decode_csv(content, self.max_bytes)
        reader = csv.DictReader(io.StringIO(text, newline=""))
        if not reader.fieldnames:
            raise IngestionValidationError(["CSV header is required"])
        fieldnames = [name.strip() if name else "" for name in reader.fieldnames]
        if len(fieldnames) != len(set(fieldnames)) or any(not name for name in fieldnames):
            raise IngestionValidationError(["CSV header contains blank or duplicate columns"])
        if not ({"url", "external_id"} & set(fieldnames)):
            raise IngestionValidationError(["CSV requires url or external_id column"])
        if not ({"title", "content_text"} & set(fieldnames)):
            raise IngestionValidationError(["CSV requires title or content_text column"])
        if "collected_at" not in fieldnames:
            raise IngestionValidationError(["CSV requires collected_at column"])

        results: list[IngestedEvidence] = []
        errors: list[str] = []
        for row_number, raw_row in enumerate(reader, start=2):
            if row_number - 1 > self.max_rows:
                errors.append(f"CSV exceeds {self.max_rows} data rows")
                break
            try:
                if None in raw_row:
                    raise IngestionValidationError(["row contains more values than the CSV header"])
                row = {(key or "").strip(): (value or "").strip() for key, value in raw_row.items()}
                results.append(
                    _build_evidence(
                        platform=platform,
                        source_key=source_key,
                        collection_run_key=collection_run_key,
                        collected_by=collected_by,
                        collected_at=_parse_datetime(row.get("collected_at", "")),
                        acquisition_mode=AcquisitionMode.CSV,
                        external_id=row.get("external_id") or None,
                        url=row.get("url") or None,
                        title=row.get("title", ""),
                        content_text=row.get("content_text", ""),
                        language_code=row.get("language_code", ""),
                        market_code=row.get("market_code", ""),
                        query=row.get("query", ""),
                        attributes={key: value for key, value in row.items() if key not in _KNOWN_FIELDS},
                        row_number=row_number,
                    )
                )
            except (ValueError, IngestionValidationError) as exc:
                errors.append(f"row {row_number}: {exc}")
        if errors:
            raise IngestionValidationError(errors)
        if not results:
            raise IngestionValidationError(["CSV contains no data rows"])
        return tuple(results)


def validate_manual_evidence(value: ManualEvidenceInput) -> IngestedEvidence:
    return _build_evidence(
        platform=value.platform,
        source_key=value.source_key,
        collection_run_key=value.collection_run_key,
        collected_by=value.collected_by,
        collected_at=value.collected_at,
        acquisition_mode=AcquisitionMode.MANUAL,
        external_id=value.external_id,
        url=value.url,
        title=value.title,
        content_text=value.content_text,
        language_code=value.language_code,
        market_code=value.market_code,
        query=value.query,
        attributes=value.attributes,
        row_number=None,
    )


def validate_connector_evidence(
    *,
    platform: Platform,
    source_key: str,
    collection_run_key: str,
    collected_by: str,
    collected_at: datetime,
    acquisition_mode: AcquisitionMode,
    item: Mapping[str, Any],
    language_code: str = "",
    market_code: str = "",
    query: str = "",
) -> IngestedEvidence:
    """Normalize one API/browser result through the same provenance rules as manual input.

    Connector transports return deliberately small mappings.  They are not
    trusted database payloads: this boundary normalizes URLs, validates text,
    computes the immutable payload digest and derives the dedupe key before a
    Daily Operations service may persist the result.
    """

    if acquisition_mode not in {AcquisitionMode.API, AcquisitionMode.BROWSER}:
        raise IngestionValidationError(["automatic connector evidence must use API or BROWSER mode"])
    if not isinstance(item, Mapping):
        raise IngestionValidationError(["connector evidence item must be an object"])
    attributes = item.get("attributes", {})
    if not isinstance(attributes, Mapping):
        raise IngestionValidationError(["connector evidence attributes must be an object"])
    item_collected_at = item.get("collected_at")
    if item_collected_at not in (None, ""):
        if not isinstance(item_collected_at, str):
            raise IngestionValidationError(["connector collected_at must be an ISO-8601 string"])
        try:
            collected_at = _parse_datetime(item_collected_at)
        except ValueError as exc:
            raise IngestionValidationError([str(exc)]) from exc
    return _build_evidence(
        platform=platform,
        source_key=source_key,
        collection_run_key=collection_run_key,
        collected_by=collected_by,
        collected_at=collected_at,
        acquisition_mode=acquisition_mode,
        external_id=item.get("external_id"),
        url=item.get("url"),
        title=item.get("title", ""),
        content_text=item.get("content_text", ""),
        language_code=language_code,
        market_code=market_code,
        query=query,
        attributes=attributes,
        row_number=None,
    )


def _build_evidence(
    *,
    platform: Platform,
    source_key: str,
    collection_run_key: str,
    collected_by: str,
    collected_at: datetime,
    acquisition_mode: AcquisitionMode,
    external_id: str | None,
    url: str | None,
    title: str,
    content_text: str,
    language_code: str,
    market_code: str,
    query: str,
    attributes: Mapping[str, Any],
    row_number: int | None,
) -> IngestedEvidence:
    if not _SOURCE_KEY.fullmatch(source_key) or not _SOURCE_KEY.fullmatch(collection_run_key):
        raise IngestionValidationError(["source_key or collection_run_key is invalid"])
    if not collected_by or len(collected_by) > 200:
        raise IngestionValidationError(["collected_by is required and must not exceed 200 characters"])
    if collected_at.tzinfo is None:
        raise IngestionValidationError(["collected_at must include a timezone"])
    external_id = _clean_optional(external_id, "external_id", 1_000)
    normalized_url = _normalize_url(url)
    if not external_id and not normalized_url:
        raise IngestionValidationError(["external_id or url is required"])
    title = _clean_text(title, "title", 5_000, required=False)
    content_text = _clean_text(content_text, "content_text", 250_000, required=False)
    if not title and not content_text:
        raise IngestionValidationError(["title or content_text is required"])
    language_code = _clean_text(language_code, "language_code", 35, required=False)
    market_code = _clean_text(market_code, "market_code", 35, required=False)
    query = _clean_text(query, "query", 2_000, required=False)
    canonical = {
        "platform": platform.value,
        "source_key": source_key,
        "collection_run_key": collection_run_key,
        "acquisition_mode": acquisition_mode.value,
        "collected_by": collected_by,
        "collected_at": collected_at.isoformat(),
        "external_id": external_id,
        "url": normalized_url,
        "title": title,
        "content_text": content_text,
        "language_code": language_code,
        "market_code": market_code,
        "query": query,
        "attributes": dict(attributes),
    }
    encoded = json.dumps(canonical, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    payload_digest = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
    natural_key = {
        "platform": platform.value,
        "source_key": source_key,
        "external_id": external_id,
        "url": normalized_url,
        "collected_at": collected_at.isoformat(),
    }
    dedupe_digest = hashlib.sha256(
        json.dumps(natural_key, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    provenance = ProvenancePayload(
        platform=platform,
        source_key=source_key,
        collection_run_key=collection_run_key,
        acquisition_mode=acquisition_mode,
        collected_by=collected_by,
        collected_at=collected_at,
        payload_digest=payload_digest,
        external_id=external_id,
        url=normalized_url,
        row_number=row_number,
    )
    return IngestedEvidence(
        platform=platform,
        external_id=external_id,
        url=normalized_url,
        title=title,
        content_text=content_text,
        language_code=language_code,
        market_code=market_code,
        query=query,
        attributes=attributes,
        provenance=provenance,
        dedupe_key=f"dk1_{dedupe_digest}",
    )


def _decode_csv(content: str | bytes, max_bytes: int) -> str:
    if isinstance(content, bytes):
        if len(content) > max_bytes:
            raise IngestionValidationError(["CSV exceeds the byte limit"])
        try:
            return content.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise IngestionValidationError(["CSV must be UTF-8 encoded"]) from exc
    encoded = content.encode("utf-8")
    if len(encoded) > max_bytes:
        raise IngestionValidationError(["CSV exceeds the byte limit"])
    return content.removeprefix("\ufeff")


def _parse_datetime(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("collected_at must be an ISO-8601 datetime") from exc
    if parsed.tzinfo is None:
        raise ValueError("collected_at must include a timezone")
    return parsed


def _normalize_url(value: str | None) -> str | None:
    value = _clean_optional(value, "url", 4_096)
    if not value:
        return None
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise IngestionValidationError(["url must be an absolute HTTP(S) URL"])
    if parsed.username or parsed.password:
        raise IngestionValidationError(["url must not contain credentials"])
    if parsed.fragment:
        raise IngestionValidationError(["url must not contain a fragment"])
    try:
        hostname = parsed.hostname.encode("idna").decode("ascii").lower()
        port = f":{parsed.port}" if parsed.port else ""
    except (UnicodeError, ValueError) as exc:
        raise IngestionValidationError(["url hostname or port is invalid"]) from exc
    return urlunsplit((parsed.scheme.lower(), f"{hostname}{port}", parsed.path or "/", parsed.query, ""))


def _clean_optional(value: str | None, name: str, maximum: int) -> str | None:
    if value is None:
        return None
    cleaned = _clean_text(value, name, maximum, required=False)
    return cleaned or None


def _clean_text(value: str, name: str, maximum: int, *, required: bool) -> str:
    if not isinstance(value, str):
        raise IngestionValidationError([f"{name} must be text"])
    value = value.strip()
    if required and not value:
        raise IngestionValidationError([f"{name} is required"])
    if len(value) > maximum:
        raise IngestionValidationError([f"{name} exceeds {maximum} characters"])
    if any(unicodedata.category(character) == "Cc" and character not in "\n\t" for character in value):
        raise IngestionValidationError([f"{name} contains a control character"])
    return value
