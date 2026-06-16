"""Live-database reference data provider for the extraction engine.

This is a plain helper class (NOT an Odoo model). It satisfies the vendored
engine's ``ReferenceDataProvider`` protocol
(``processing/db/protocol.py``) by reading candidate names from real Odoo
records, so the FuzzyResolver / veracity pass can validate extracted fields
against the live database instead of the static fixtures.

Master-data models (workers, companies, equipment, parts…) may not all exist
in a given deployment yet, so every lookup is guarded: if the target model is
not in the registry, the entity simply yields no candidates (the field then
stays unresolved — honest, not a crash). Refine ``_MODEL_MAP`` as the real
master-data models come online.
"""

import logging

_logger = logging.getLogger(__name__)


class OdooReferenceDataProvider:
    """Implements the engine's ReferenceDataProvider against Odoo records."""

    # entity -> (model, name_field, domain)
    _MODEL_MAP = {
        "worker":            ("hr.employee",     "name", []),
        "company":           ("res.partner",     "name", [("is_company", "=", True)]),
        "location":          ("res.partner",     "name", []),
        "vehicle_equipment": ("fleet.vehicle",   "name", []),
        "parts_used":        ("product.product", "name", []),
    }

    def __init__(self, env):
        self.env = env

    def _model(self, entity):
        """Return a sudo()'d recordset for *entity*, or None if unavailable."""
        spec = self._MODEL_MAP.get(entity)
        if not spec:
            return None, None, None
        model_name, name_field, domain = spec
        if model_name not in self.env.registry:
            return None, None, None
        try:
            return self.env[model_name].sudo(), name_field, domain
        except Exception:  # access / registry edge cases
            _logger.warning("Reference model %s unavailable for %s", model_name, entity)
            return None, None, None

    def get_names(self, entity: str) -> list[str]:
        model, name_field, domain = self._model(entity)
        if model is None:
            return []
        try:
            records = model.search(domain)
            names = [r[name_field] for r in records if r[name_field]]
            # De-duplicate while preserving order.
            seen, out = set(), []
            for n in names:
                if n not in seen:
                    seen.add(n)
                    out.append(n)
            return out
        except Exception:
            _logger.exception("Failed reading reference names for %s", entity)
            return []

    def resolve_canonical(self, entity: str, matched_name: str) -> dict | None:
        model, name_field, domain = self._model(entity)
        if model is None:
            return None
        try:
            rec = model.search(domain + [(name_field, "=", matched_name)], limit=1)
            if not rec:
                return None
            return {"id": rec.id, "name": rec[name_field], "model": model._name}
        except Exception:
            _logger.exception("Failed resolving canonical %s for %r", entity, matched_name)
            return None
