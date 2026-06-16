"""Seed / refresh the editable AI prompt & schema records from vendored files.

Shared by the install ``post_init_hook`` and the upgrade migration so both paths
populate ``unifix.ai.prompt`` / ``unifix.ai.schema`` from the on-disk
``processing/prompts`` and ``processing/schemas`` files without duplicating the
file contents into XML. Seeding is idempotent: a record that already exists
(and may have been edited in the UI) is never overwritten.
"""

import os

_HERE = os.path.dirname(__file__)
_PROMPTS_DIR = os.path.join(_HERE, "processing", "prompts")
_SCHEMAS_DIR = os.path.join(_HERE, "processing", "schemas")
_DOCTYPES_DIR = os.path.join(_HERE, "processing", "config", "document_types")


def document_config_map():
    """Map each engine document config to the prompt/schema it uses.

    Returns a list of ``{"config", "prompt", "schema"}`` dicts read from
    ``processing/config/document_types/*.yaml``. ``config`` is the file stem
    (e.g. ``audio_v1``, ``image_v3``) — the precise document type, since several
    configs can share the same ``document_type`` (audio_v1 / audio_v1_fr). This
    is the single source of truth for "which prompt embeds which schema".
    """
    import yaml

    out = []
    if not os.path.isdir(_DOCTYPES_DIR):
        return out
    for fn in sorted(os.listdir(_DOCTYPES_DIR)):
        if not fn.endswith((".yaml", ".yml")):
            continue
        with open(os.path.join(_DOCTYPES_DIR, fn), "r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}
        out.append({
            "config": os.path.splitext(fn)[0],
            "prompt": data.get("prompt"),
            "schema": data.get("schema_version"),
        })
    return out


def read_prompt_file(name):
    """Return the text of prompts/<name>.txt, or None if absent."""
    path = os.path.join(_PROMPTS_DIR, "%s.txt" % name)
    if not os.path.isfile(path):
        return None
    with open(path, "r", encoding="utf-8") as fh:
        return fh.read()


def read_schema_file(version):
    """Return the text of schemas/work_order_<version>.json, or None if absent."""
    path = os.path.join(_SCHEMAS_DIR, "work_order_%s.json" % version)
    if not os.path.isfile(path):
        return None
    with open(path, "r", encoding="utf-8") as fh:
        return fh.read()


def _iter_prompt_names():
    if not os.path.isdir(_PROMPTS_DIR):
        return
    for fn in sorted(os.listdir(_PROMPTS_DIR)):
        if fn.endswith(".txt"):
            yield fn[:-len(".txt")]


def _iter_schema_versions():
    if not os.path.isdir(_SCHEMAS_DIR):
        return
    prefix, suffix = "work_order_", ".json"
    for fn in sorted(os.listdir(_SCHEMAS_DIR)):
        if fn.startswith(prefix) and fn.endswith(suffix):
            yield fn[len(prefix):-len(suffix)]


def seed_ai_records(env):
    """Create a DB record for every vendored prompt/schema not already present."""
    Prompt = env["unifix.ai.prompt"].sudo()
    for name in _iter_prompt_names():
        if not Prompt.search_count([("name", "=", name)]):
            Prompt.create({"name": name, "body": read_prompt_file(name)})

    Schema = env["unifix.ai.schema"].sudo()
    for version in _iter_schema_versions():
        if not Schema.search_count([("name", "=", version)]):
            Schema.create({"name": version, "body": read_schema_file(version)})

    # Link each extraction prompt to the schema it embeds, per the engine
    # document configs. Only fills empty links so manual UI edits are preserved.
    for entry in document_config_map():
        if not entry["prompt"] or not entry["schema"]:
            continue
        prompt = Prompt.search([("name", "=", entry["prompt"])], limit=1)
        schema = Schema.search([("name", "=", entry["schema"])], limit=1)
        if prompt and schema and not prompt.schema_id:
            prompt.schema_id = schema.id
