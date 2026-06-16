"""Link each AI prompt to its associated schema when upgrading to 19.0.4.0.0.

``seed_ai_records`` is idempotent and now also fills ``unifix.ai.prompt.schema_id``
from the engine document configs (only where empty, so UI edits are preserved).
Running it here links the prompts on databases that were seeded before the
``schema_id`` field existed.
"""

from odoo import api, SUPERUSER_ID
from odoo.addons.unifix_odoo.ai_seed import seed_ai_records


def migrate(cr, version):
    env = api.Environment(cr, SUPERUSER_ID, {})
    seed_ai_records(env)
