"""Seed the editable AI prompt/schema records when upgrading to 19.0.3.0.0.

post_init_hook only runs on a fresh install, so existing databases get their
``unifix.ai.prompt`` / ``unifix.ai.schema`` records populated here instead.
Seeding is idempotent — already-present (possibly edited) records are kept.
"""

from odoo import api, SUPERUSER_ID
from odoo.addons.unifix_odoo.ai_seed import seed_ai_records


def migrate(cr, version):
    env = api.Environment(cr, SUPERUSER_ID, {})
    seed_ai_records(env)
