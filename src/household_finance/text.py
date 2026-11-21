"""Text helpers shared by the database layer and importers."""

from __future__ import annotations

import unicodedata


def name_key(name: str) -> str:
    """Case- and accent-insensitive comparison key for names.

    Stored in the name_key columns, which carry the uniqueness constraints,
    so "Café" and "cafe" count as the same name.
    """
    decomposed = unicodedata.normalize("NFKD", name)
    stripped = "".join(c for c in decomposed if not unicodedata.combining(c))
    return stripped.casefold()
