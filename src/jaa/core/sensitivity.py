"""Which profile fields, preferences and questions are sensitive by default.

Sensitive values are still stored locally, but are flagged so that review, export and
answer reuse can treat them with extra care (SPEC §6, §8). Callers may flag more.
"""

from typing import Final

SENSITIVE_KEY_PREFIXES: Final = (
    "contact.",
    "identity.",
    "work_authorization",
    "sponsorship",
    "salary",
    "compensation",
    "demographic",
    "disability",
    "veteran",
    "criminal",
    "medical",
    "health",
    "consent",
    "legal",
    "date_of_birth",
    "nationality",
)


def is_sensitive_key(key: str) -> bool:
    """True if a fact field key, preference key or question key is sensitive by default."""
    normalized = key.strip().lower()
    return normalized.startswith(SENSITIVE_KEY_PREFIXES)
