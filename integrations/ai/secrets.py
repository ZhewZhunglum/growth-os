from __future__ import annotations

import unicodedata
from dataclasses import dataclass
from pathlib import Path

from integrations.errors import SecretLoadingError


@dataclass(frozen=True, slots=True)
class SecretFileReference:
    path: Path

    def __str__(self) -> str:
        return "SecretFileReference(<redacted>)"

    def __repr__(self) -> str:
        return "SecretFileReference(path=<redacted>)"


def read_secret_file(reference: SecretFileReference, *, max_bytes: int = 16_384) -> str:
    path = reference.path
    try:
        if path.is_symlink():
            raise SecretLoadingError("Secret file must not be a symbolic link")
        if not path.is_file():
            raise SecretLoadingError("Secret file does not exist or is not a regular file")
        if path.stat().st_size > max_bytes:
            raise SecretLoadingError("Secret file exceeds the configured size limit")
        value = path.read_text(encoding="utf-8").strip()
    except SecretLoadingError:
        raise
    except (OSError, UnicodeError) as exc:
        raise SecretLoadingError("Secret file could not be read") from exc

    if not value:
        raise SecretLoadingError("Secret file is empty")
    if any(unicodedata.category(character) == "Cc" for character in value):
        raise SecretLoadingError("Secret contains a control character")
    return value
