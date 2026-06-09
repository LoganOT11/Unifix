"""Video keyframe model.

Stores extracted still frames at timestamps identified by Gemini.
Each keyframe is derived data (an image, not the video) — permitted by Constraint #1.
"""

from odoo import models, fields


class UnifixVideoKeyframe(models.Model):
    _name = 'unifix.video.keyframe'
    _description = 'Unifix Video Keyframe'
    _order = 'job_id, sequence, timestamp'

    job_id = fields.Many2one(
        'unifix.video.job', required=True,
        ondelete='cascade', index=True,
    )
    segment_id = fields.Many2one(
        'unifix.video.segment', ondelete='set null',
        help="Nearest transcript segment",
    )
    sequence = fields.Integer(default=10)
    timestamp = fields.Float(required=True, help="Time position in seconds")
    timestamp_display = fields.Char(help="Formatted timestamp (mm:ss)")
    image = fields.Image(max_width=1280, max_height=720)
    image_filename = fields.Char()
    reason = fields.Char(help="Why this moment was selected (from Gemini)")
