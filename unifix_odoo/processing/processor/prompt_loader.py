"""Load prompt templates and JSON schemas.

By default these come from the on-disk ``prompts/`` and ``schemas/`` directories.
A host application (e.g. the Odoo module) may register an *override resolver* via
:func:`set_override_resolver` to serve editable, DB-backed copies instead. The
resolver is a plain callable ``(kind, name) -> str | None`` where ``kind`` is
``"prompt"`` or ``"schema"``; returning ``None`` (or raising) falls back to the
file. This keeps the engine framework-free — it knows nothing about Odoo.
"""

import json
from pathlib import Path
from typing import Callable

_PROMPTS_DIR = Path(__file__).resolve().parent.parent / "prompts"
_SCHEMAS_DIR = Path(__file__).resolve().parent.parent / "schemas"

# Optional host-supplied resolver. None → pure file-based loading (CLI/tests).
_OVERRIDE_RESOLVER: Callable[[str, str], str | None] | None = None


def set_override_resolver(fn: Callable[[str, str], str | None] | None) -> None:
    """Install (or clear, with ``None``) the prompt/schema override resolver."""
    global _OVERRIDE_RESOLVER
    _OVERRIDE_RESOLVER = fn


def _resolve_override(kind: str, name: str) -> str | None:
    """Return an overriding body for ``(kind, name)`` or None to use the file.

    Best-effort: any resolver error degrades silently to the file source so a
    misconfigured override can never break extraction.
    """
    if _OVERRIDE_RESOLVER is None:
        return None
    try:
        return _OVERRIDE_RESOLVER(kind, name)
    except Exception:  # noqa: BLE001 — never let an override failure escape
        return None


def load_schema(version: str) -> dict:
    """Load the work-order JSON schema for ``version`` (override-aware).

    This is the single source of truth for schema bytes so the prompt's
    ``{schema_block}`` and the downstream validator stay in lockstep.
    """
    override = _resolve_override("schema", version)
    if override is not None:
        return override if isinstance(override, dict) else json.loads(override)
    path = _SCHEMAS_DIR / f"work_order_{version}.json"
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def _render_schema_block(schema_version: str, fmt: str = "simplified") -> str:
    schema = load_schema(schema_version)
    if fmt == "full":
        return json.dumps(schema, indent=2)
    return json.dumps(
        {n: p.get("type", "string") for n, p in schema["properties"].items()},
        indent=2,
    )


def load_prompt(
    name: str,
    schema_version: str | None = None,
    schema_format: str = "simplified",
) -> str:
    """
    Load a prompt template by name (without .txt extension).

    If schema_version is provided and the template contains {schema_block},
    the placeholder is replaced with a rendered view of the named schema.
    schema_format: "simplified" emits {field: type} dict; "full" emits the
    complete JSON schema document.
    """
    override = _resolve_override("prompt", name)
    template = override if override is not None else (
        _PROMPTS_DIR / f"{name}.txt").read_text(encoding="utf-8")
    if schema_version and "{schema_block}" in template:
        block = _render_schema_block(schema_version, fmt=schema_format)
        return template.replace("{schema_block}", block)
    return template
