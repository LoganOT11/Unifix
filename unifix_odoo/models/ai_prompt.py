"""Editable, DB-backed AI prompts and JSON schemas.

The vendored engine loads prompts/schemas from files by default. These models
let an administrator view and edit the exact text in use from the Odoo backend,
with full change history (mail.thread).

A thread-local resolver is registered with the engine's ``prompt_loader``: while
a pipeline runs (see ``workorder_job._run_pipeline``, which binds the env via
:func:`bind_resolver_env`), the engine asks these models first and falls back to
the on-disk file only when no active record exists. Outside that bound window the
resolver returns ``None`` so nothing else (CLI, tests) is affected.
"""

import json
import logging
import threading
from contextlib import contextmanager

from odoo import models, fields, api, _
from odoo.exceptions import UserError, ValidationError

from processor.prompt_loader import set_override_resolver

_logger = logging.getLogger(__name__)

# Odoo can run several worker threads in one process, so the env the resolver
# reads from must be thread-local — never a plain module global.
_local = threading.local()


@contextmanager
def bind_resolver_env(env):
    """Bind *env* for the engine's prompt/schema resolver for the duration of a
    pipeline run, restoring the previous binding on exit.

    Also starts a fresh usage tally so the caller can read back exactly which
    prompt and schema names the engine asked for during the run (see
    :func:`current_usage`)."""
    prev_env = getattr(_local, "env", None)
    prev_used = getattr(_local, "used", None)
    _local.env = env
    _local.used = {"prompt": [], "schema": []}
    try:
        yield
    finally:
        _local.env = prev_env
        _local.used = prev_used


def current_usage():
    """Names requested through the resolver in the active bound window, as
    ``{"prompt": [...], "schema": [...]}`` — or None outside a run. Read this
    *inside* the ``bind_resolver_env`` block before it restores the previous
    tally."""
    return getattr(_local, "used", None)


def _resolver(kind, name):
    """Engine hook → DB body for ``(kind, name)`` or None to use the file."""
    env = getattr(_local, "env", None)
    if env is None:
        return None
    # Record the name regardless of DB hit/file fallback — this is the truthful
    # record of which prompt/schema the engine actually used for this run.
    used = getattr(_local, "used", None)
    if isinstance(used, dict) and kind in used and name not in used[kind]:
        used[kind].append(name)
    model = {"prompt": "unifix.ai.prompt", "schema": "unifix.ai.schema"}.get(kind)
    if not model:
        return None
    rec = env[model].sudo().search(
        [("name", "=", name), ("active", "=", True)], limit=1)
    return rec.body if rec and rec.body else None


# Register once at import. unifix_odoo/__init__.py puts processing/ on sys.path
# before importing models, so prompt_loader is importable here.
set_override_resolver(_resolver)


class UnifixAiPrompt(models.Model):
    _name = "unifix.ai.prompt"
    _description = "Unifix AI Prompt Template"
    _inherit = ["mail.thread"]
    _order = "name"

    name = fields.Char(
        required=True, index=True, tracking=True,
        help="Prompt key, matching the engine file stem "
             "(e.g. 'audio_extraction_v2').")
    description = fields.Char(tracking=True)
    body = fields.Text(
        required=True, tracking=True,
        help="The full prompt template. The {schema_block} placeholder is "
             "substituted with the linked schema at runtime.")
    active = fields.Boolean(default=True, tracking=True)

    schema_id = fields.Many2one(
        "unifix.ai.schema", string="Associated schema", tracking=True,
        ondelete="set null",
        help="The schema rendered into this prompt's {schema_block} and used to "
             "validate the output. Seeded from the engine document configs.")
    has_schema_block = fields.Boolean(compute="_compute_has_schema_block")
    # Edit the linked schema's JSON right here, alongside the prompt.
    schema_body = fields.Text(
        related="schema_id.body", readonly=False, string="Schema JSON")
    applies_to = fields.Char(
        compute="_compute_applies_to", string="Used by document types",
        help="Engine document configs whose extraction uses this prompt.")

    _name_uniq = models.Constraint(
        "unique(name)", "A prompt with this name already exists.")

    @api.depends("body")
    def _compute_has_schema_block(self):
        for rec in self:
            rec.has_schema_block = bool(rec.body and "{schema_block}" in rec.body)

    @api.depends("name")
    def _compute_applies_to(self):
        from odoo.addons.unifix_odoo.ai_seed import document_config_map
        by_prompt = {}
        for entry in document_config_map():
            if entry["prompt"]:
                by_prompt.setdefault(entry["prompt"], []).append(entry["config"])
        for rec in self:
            rec.applies_to = ", ".join(by_prompt.get(rec.name, [])) or ""

    def action_reload_from_file(self):
        """Reset body from the vendored prompts/<name>.txt (discards edits)."""
        from odoo.addons.unifix_odoo.ai_seed import read_prompt_file
        for rec in self:
            text = read_prompt_file(rec.name)
            if text is None:
                raise UserError(_("No prompt file found for '%s'.") % rec.name)
            rec.body = text
        return True


class UnifixAiSchema(models.Model):
    _name = "unifix.ai.schema"
    _description = "Unifix AI JSON Schema"
    _inherit = ["mail.thread"]
    _order = "name"

    name = fields.Char(
        required=True, index=True, tracking=True,
        help="Schema version key (e.g. 'v1', 'v3', 'v1_fr'), matching "
             "schemas/work_order_<name>.json.")
    description = fields.Char(tracking=True)
    body = fields.Text(
        required=True, tracking=True,
        help="The JSON schema document. Must be valid JSON.")
    active = fields.Boolean(default=True, tracking=True)

    prompt_ids = fields.One2many(
        "unifix.ai.prompt", "schema_id", string="Used by prompts")
    applies_to = fields.Char(
        compute="_compute_applies_to", string="Used by document types",
        help="Engine document configs whose extraction uses this schema.")

    _name_uniq = models.Constraint(
        "unique(name)", "A schema with this name already exists.")

    @api.depends("name")
    def _compute_applies_to(self):
        from odoo.addons.unifix_odoo.ai_seed import document_config_map
        by_schema = {}
        for entry in document_config_map():
            if entry["schema"]:
                by_schema.setdefault(entry["schema"], []).append(entry["config"])
        for rec in self:
            rec.applies_to = ", ".join(by_schema.get(rec.name, [])) or ""

    @api.constrains("body")
    def _check_valid_json(self):
        for rec in self:
            if rec.body:
                try:
                    json.loads(rec.body)
                except ValueError as exc:
                    raise ValidationError(
                        _("Schema '%s' is not valid JSON: %s") % (rec.name, exc))

    def action_reload_from_file(self):
        """Reset body from the vendored schemas/work_order_<name>.json."""
        from odoo.addons.unifix_odoo.ai_seed import read_schema_file
        for rec in self:
            text = read_schema_file(rec.name)
            if text is None:
                raise UserError(_("No schema file found for '%s'.") % rec.name)
            rec.body = text
        return True
