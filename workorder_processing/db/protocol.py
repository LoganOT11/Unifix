"""Reference data provider protocol."""

from typing import Protocol, runtime_checkable


@runtime_checkable
class ReferenceDataProvider(Protocol):
    def get_names(self, entity: str) -> list[str]:
        """Return all known names/strings for an entity type."""

    def resolve_canonical(self, entity: str, matched_name: str) -> dict | None:
        """Return the canonical DB record for a matched name, or None."""
