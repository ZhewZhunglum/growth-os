from __future__ import annotations

from typing import Any


CONTENT_DRAFT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "platform",
        "content_type",
        "title",
        "hook",
        "body",
        "call_to_action",
        "hashtags",
        "production_notes",
        "claim_keys",
        "evidence_ids",
        "language_code",
    ],
    "properties": {
        "platform": {"type": "string", "minLength": 1, "maxLength": 64},
        "content_type": {"type": "string", "minLength": 1, "maxLength": 80},
        "title": {"type": "string", "minLength": 1, "maxLength": 300},
        "hook": {"type": "string", "minLength": 1, "maxLength": 1000},
        "body": {"type": "string", "minLength": 1, "maxLength": 30000},
        "call_to_action": {"type": "string", "minLength": 1, "maxLength": 1000},
        "hashtags": {
            "type": "array",
            "maxItems": 30,
            "items": {"type": "string", "minLength": 1, "maxLength": 100},
        },
        "production_notes": {"type": "string", "maxLength": 10000},
        "claim_keys": {
            "type": "array",
            "maxItems": 100,
            "items": {"type": "string", "minLength": 1, "maxLength": 120},
        },
        "evidence_ids": {
            "type": "array",
            "minItems": 1,
            "maxItems": 100,
            "items": {"type": "string", "format": "uuid"},
        },
        "language_code": {"type": "string", "minLength": 2, "maxLength": 16},
    },
}


PLATFORM_CONTENT_TYPES = {
    "TIKTOK": "short-video-script",
    "PINTEREST": "pin-copy",
    "QUORA": "answer-draft",
    "SHOPIFY": "blog-draft",
    "GOOGLE_SEARCH": "seo-content-brief",
    "GOOGLE_SEARCH_CONSOLE": "search-optimization-brief",
    "GOOGLE_ANALYTICS_4": "measurement-action-brief",
}


def render_complete_content(output: dict[str, Any]) -> str:
    """Render one human-editable, publication-ready text body.

    Structured fields remain in metadata for audit and future platform adapters;
    the inline text is what an employee reviews, edits, copies, and publishes.
    """

    hashtags = " ".join(
        tag if str(tag).startswith("#") else f"#{str(tag).lstrip('#')}"
        for tag in output.get("hashtags", [])
        if str(tag).strip()
    )
    sections = [
        str(output["title"]).strip(),
        str(output["hook"]).strip(),
        str(output["body"]).strip(),
        str(output["call_to_action"]).strip(),
    ]
    if hashtags:
        sections.append(hashtags)
    # Production notes are workflow metadata, not publishable copy.  Keeping
    # them out of the exact inline asset prevents copy/paste and API/browser
    # transports from accidentally publishing internal instructions.
    return "\n\n".join(section for section in sections if section)
