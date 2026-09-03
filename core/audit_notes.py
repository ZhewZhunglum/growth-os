from __future__ import annotations


USER_PROVIDED_TAG = "[USER_PROVIDED]"
SYSTEM_DEFAULT_TAG = "[SYSTEM_DEFAULT]"


def tag_optional_audit_note(
    value: object,
    *,
    default: str,
    existing_value: object | None = None,
) -> str:
    """Return a non-empty, source-tagged note for append-only audit facts.

    Ordinary workflow explanations are optional in the UI.  The stored fact is
    still explicit about whether a person wrote the note or the system supplied
    a neutral default.  A stored value is returned unchanged only for an exact
    command replay. Reserved tags supplied by a new external request remain
    user-provided text, so callers cannot impersonate a system-generated note.
    """

    text = str(value or "").strip()
    # Preserve the original payload for retries of commands written before
    # source tags existed.  Otherwise an exact legacy retry would look like a
    # conflicting new command merely because the audit convention improved.
    existing_text = str(existing_value or "").strip()
    if existing_value is not None and existing_text == text:
        return existing_text
    if text:
        return f"{USER_PROVIDED_TAG} {text}"
    return f"{SYSTEM_DEFAULT_TAG} {default.strip()}"


def audit_note_source(value: object) -> str:
    text = str(value or "").strip()
    if text.startswith(USER_PROVIDED_TAG):
        return "USER_PROVIDED"
    if text.startswith(SYSTEM_DEFAULT_TAG):
        return "SYSTEM_DEFAULT"
    return "LEGACY"


def audit_note_text(value: object) -> str:
    text = str(value or "").strip()
    for tag in (USER_PROVIDED_TAG, SYSTEM_DEFAULT_TAG):
        if text.startswith(tag):
            return text[len(tag) :].lstrip()
    return text
