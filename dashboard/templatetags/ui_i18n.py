"""Presentation-only Chinese/English helpers.

The selected language comes from Django's session-backed LocaleMiddleware.
These tags never translate stored business values, enums, audit text, task
contracts, review decisions, or any other database payload.
"""

from django import template


register = template.Library()


def _is_english(context) -> bool:
    request = context.get("request")
    language_code = getattr(request, "LANGUAGE_CODE", "zh-hans")
    return str(language_code).lower().startswith("en")


@register.simple_tag(takes_context=True)
def ui(context, chinese: str, english: str) -> str:
    """Return one of two static UI labels for the current session."""

    return english if _is_english(context) else chinese


@register.simple_tag(takes_context=True)
def ui_language_code(context) -> str:
    """Return a valid HTML language tag without exposing business locale."""

    return "en" if _is_english(context) else "zh-CN"
