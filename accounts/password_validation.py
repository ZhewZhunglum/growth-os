from __future__ import annotations

import unicodedata

from django.core.exceptions import ValidationError
from django.utils.translation import gettext as _


class NonBlankAndNoControlCharactersValidator:
    """Reject passwords that are only whitespace or contain control characters."""

    def validate(self, password, user=None):
        if not password or not password.strip():
            raise ValidationError(
                _("This password cannot be empty or contain only whitespace."),
                code="password_blank_or_whitespace",
            )
        if any(unicodedata.category(character) == "Cc" for character in password):
            raise ValidationError(
                _("This password cannot contain control characters."),
                code="password_contains_control_character",
            )

    def get_help_text(self):
        return _("Your password cannot be only whitespace or contain control characters.")
