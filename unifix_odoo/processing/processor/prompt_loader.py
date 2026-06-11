"""Load prompt templates from the prompts/ directory."""

import json
from pathlib import Path

_PROMPTS_DIR = Path(__file__).resolve().parent.parent / "prompts"
_SCHEMAS_DIR = Path(__file__).resolve().parent.parent / "schemas"


def _render_schema_block(schema_version: str, fmt: str = "simplified") -> str:
    path = _SCHEMAS_DIR / f"work_order_{schema_version}.json"
    with open(path, "r", encoding="utf-8") as fh:
        schema = json.load(fh)
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
    template = (_PROMPTS_DIR / f"{name}.txt").read_text(encoding="utf-8")
    if schema_version and "{schema_block}" in template:
        block = _render_schema_block(schema_version, fmt=schema_format)
        return template.replace("{schema_block}", block)
    return template
