from __future__ import annotations

from typing import Any


DAILY_ANALYSIS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "topic_label",
        "summary",
        "search_intent",
        "pain_points",
        "job_to_be_done",
        "demand_score",
        "velocity_score",
        "confidence",
        "fit_score",
        "evidence_strength",
        "recommendation",
        "priority_score",
        "risk_level",
    ],
    "properties": {
        "topic_label": {"type": "string", "minLength": 1, "maxLength": 240},
        "summary": {"type": "string", "minLength": 1, "maxLength": 5000},
        "search_intent": {"type": "string", "maxLength": 80},
        "pain_points": {
            "type": "array",
            "items": {"type": "string", "minLength": 1, "maxLength": 500},
            "maxItems": 20,
        },
        "job_to_be_done": {"type": "string", "maxLength": 2000},
        "demand_score": {"type": "number", "minimum": 0, "maximum": 1},
        "velocity_score": {"type": "number", "minimum": 0, "maximum": 1},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "fit_score": {"type": "number", "minimum": 0, "maximum": 1},
        "evidence_strength": {"type": "number", "minimum": 0, "maximum": 1},
        "recommendation": {"type": "string", "minLength": 1, "maxLength": 5000},
        "priority_score": {"type": "number", "minimum": 0, "maximum": 1},
        "risk_level": {"type": "string", "enum": ["LOW", "MEDIUM", "HIGH", "CRITICAL"]},
    },
}


def deterministic_analysis(*, query: str, evidence_count: int, first_title: str) -> dict[str, Any]:
    """Safe offline proposal used when live AI is deliberately disabled.

    It is clearly marked by the provider as a dry run and still requires a
    separate human acceptance before any Opportunity can enter planning.
    """

    label = (query or first_title or "Daily demand signal").strip()[:240]
    confidence = min(0.8, 0.25 + evidence_count * 0.1)
    return {
        "topic_label": label,
        "summary": f"{evidence_count} item(s) were collected for '{label}'. Human validation is required.",
        "search_intent": "research",
        "pain_points": [label],
        "job_to_be_done": f"Understand what people need when they search for {label}.",
        "demand_score": min(1.0, 0.35 + evidence_count * 0.08),
        "velocity_score": 0.25,
        "confidence": confidence,
        "fit_score": 0.5,
        "evidence_strength": confidence,
        "recommendation": "Review the linked evidence, then choose an appropriate channel response.",
        "priority_score": min(1.0, 0.4 + evidence_count * 0.06),
        "risk_level": "MEDIUM",
    }
