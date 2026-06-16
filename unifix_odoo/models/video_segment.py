"""Timestamped transcript segment model.

Each segment represents a time-aligned portion of the video transcript,
as returned by Gemini with start/end timestamps.
"""

from odoo import models, fields


class UnifixVideoSegment(models.Model):
    _name = 'unifix.video.segment'
    _description = 'Unifix Video Transcript Segment'
    _order = 'job_id, sequence, start_time'

    job_id = fields.Many2one(
        'unifix.workorder.job', required=True,
        ondelete='cascade', index=True,
    )
    sequence = fields.Integer(default=10)
    start_time = fields.Float(help="Segment start time in seconds")
    end_time = fields.Float(help="Segment end time in seconds")
    start_time_display = fields.Char(help="Formatted start (mm:ss)")
    end_time_display = fields.Char(help="Formatted end (mm:ss)")
    text = fields.Text(required=True, help="Segment transcript text")
    confidence = fields.Float(help="ASR confidence (0.0–1.0)")
