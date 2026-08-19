from __future__ import annotations

import secrets
import time
import uuid


def uuid7() -> uuid.UUID:
    """Generate an RFC 9562 UUIDv7 using the system clock and secure randomness."""

    unix_ms = time.time_ns() // 1_000_000
    if unix_ms >= 1 << 48:
        raise OverflowError("Unix millisecond timestamp does not fit UUIDv7")

    random_a = secrets.randbits(12)
    random_b = secrets.randbits(62)
    value = (unix_ms << 80) | (0x7 << 76) | (random_a << 64) | (0b10 << 62) | random_b
    return uuid.UUID(int=value)

