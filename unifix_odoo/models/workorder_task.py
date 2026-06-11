"""Work-order task line — one row of the central task table on an image form.

Populated for image jobs (schema v3 ``tasks[]``). Each row carries the unit,
time block, description, and work-order reference as written on the form.
"""

from odoo import models, fields


class UnifixWorkorderTask(models.Model):
    _name = 'unifix.workorder.task'
    _description = 'Unifix Work Order Task Line'
    _order = 'job_id, sequence'

    job_id = fields.Many2one(
        'unifix.workorder.job', required=True,
        ondelete='cascade', index=True,
    )
    sequence = fields.Integer(default=10)
    unit = fields.Char(help="Equipment/asset unit number (Unité column)")
    time_start = fields.Char()
    time_end = fields.Char()
    time_total = fields.Char(help="Duration as written, e.g. '3h', '.30'")
    description = fields.Text()
    work_order_ref = fields.Char(help="BT-/work-ticket reference for this task")
