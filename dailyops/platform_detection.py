from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlparse

from integrations.connectors.types import Platform


@dataclass(frozen=True, slots=True)
class PlatformDetection:
    platform: Platform | None
    is_url: bool


_HOST_RULES: tuple[tuple[tuple[str, ...], Platform], ...] = (
    (("pinterest.com", "pin.it"), Platform.PINTEREST),
    (("quora.com",), Platform.QUORA),
    (("tiktok.com",), Platform.TIKTOK),
    (("myshopify.com", "shopify.com"), Platform.SHOPIFY),
    (("search.google.com",), Platform.GOOGLE_SEARCH_CONSOLE),
    (("analytics.google.com",), Platform.GOOGLE_ANALYTICS_4),
    (("google.com",), Platform.GOOGLE_SEARCH),
)


def detect_platform(reference: str) -> PlatformDetection:
    """Detect an exact supported platform from a pasted URL.

    Non-URL references are intentionally not guessed.  A content ID, title,
    or account name is ambiguous across platforms and therefore requires the
    user to choose the platform explicitly.
    """

    value = reference.strip()
    if not value:
        return PlatformDetection(platform=None, is_url=False)

    candidate = value if "://" in value else f"https://{value}"
    parsed = urlparse(candidate)
    hostname = (parsed.hostname or "").lower().rstrip(".")
    looks_like_url = bool(parsed.hostname and ("://" in value or "." in value.split("/", 1)[0]))
    if not looks_like_url:
        return PlatformDetection(platform=None, is_url=False)

    for domains, platform in _HOST_RULES:
        if any(hostname == domain or hostname.endswith(f".{domain}") for domain in domains):
            return PlatformDetection(platform=platform, is_url=True)
    return PlatformDetection(platform=None, is_url=True)
