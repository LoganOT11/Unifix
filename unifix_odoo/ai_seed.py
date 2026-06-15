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
