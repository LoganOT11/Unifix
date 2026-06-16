"""In-memory reference data provider backed by the static fixture data."""

from .reference_data import (
    WORKERS, COMPANIES, LOCATIONS, EQUIPMENT, PARTS,
)


class InMemoryProvider:
    """Implements ReferenceDataProvider using the static fixture lists."""

    def get_names(self, entity: str) -> list[str]:
        if entity == "worker":
            return [w["name"] for w in WORKERS]
        if entity == "company":
            return [c["name"] for c in COMPANIES] + [c["short"] for c in COMPANIES]
        if entity == "location":
            return [loc["name"] for loc in LOCATIONS]
        if entity == "vehicle_equipment":
            tags  = [e["tag"] for e in EQUIPMENT]
            descs = [e["description"] for e in EQUIPMENT]
            combined = [f"{e['tag']} — {e['description']}" for e in EQUIPMENT]
            return tags + descs + combined
        if entity == "parts_used":
            numbers  = [p["part_number"] for p in PARTS]
            descs    = [p["description"] for p in PARTS]
            combined = [f"{p['part_number']} — {p['description']}" for p in PARTS]
            return numbers + descs + combined
        return []

    def resolve_canonical(self, entity: str, matched_name: str) -> dict | None:
        if entity == "worker":
            return next((w for w in WORKERS if w["name"] == matched_name), None)
        if entity == "company":
            return next(
                (c for c in COMPANIES if c["name"] == matched_name or c["short"] == matched_name),
                None,
            )
        if entity == "location":
            return next((loc for loc in LOCATIONS if loc["name"] == matched_name), None)
        if entity == "vehicle_equipment":
            return next(
                (e for e in EQUIPMENT
                 if matched_name in (e["tag"], e["description"],
                                     f"{e['tag']} — {e['description']}")),
                None,
            )
        if entity == "parts_used":
            return next(
                (p for p in PARTS
                 if matched_name in (p["part_number"], p["description"],
                                     f"{p['part_number']} — {p['description']}")),
                None,
            )
        return None
