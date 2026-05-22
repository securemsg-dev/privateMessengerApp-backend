from __future__ import annotations
"""
app/core/private_number.py
───────────────────────────
System-generated 10-digit identifier used in place of a real phone number.

Format: 10 digits, first digit 1-9 (no leading zero).
Stored raw (e.g. "6616970053"). Display formatting (e.g. "66-1697-0053") is
client-side concern.
"""

import secrets

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession


PRIVATE_NUMBER_LENGTH = 10


def generate_private_number() -> str:
    """Return a random 10-digit string with first digit 1-9."""
    first = secrets.randbelow(9) + 1  # 1..9
    rest = secrets.randbelow(10 ** (PRIVATE_NUMBER_LENGTH - 1))
    return f"{first}{rest:0{PRIVATE_NUMBER_LENGTH - 1}d}"


def format_private_number(raw: str) -> str:
    """Display helper: '6616970053' -> '66-1697-0053'."""
    if len(raw) != PRIVATE_NUMBER_LENGTH or not raw.isdigit():
        return raw
    return f"{raw[0:2]}-{raw[2:6]}-{raw[6:10]}"


async def generate_unique_private_number(
    db: AsyncSession, max_attempts: int = 10
) -> str:
    """
    Generate a private number that is not yet in the users table.
    Collision-retry up to `max_attempts` times. Raises RuntimeError on
    exhaustion (effectively never happens in a 10-billion keyspace).
    """
    from app.db.models.user import User

    for _ in range(max_attempts):
        candidate = generate_private_number()
        result = await db.execute(
            select(User.id).where(User.private_number == candidate)
        )
        if result.scalar_one_or_none() is None:
            return candidate
    raise RuntimeError(
        f"Failed to generate unique private_number after {max_attempts} attempts"
    )
