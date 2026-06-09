"""Configuration settings for Unifix Video Processing.

Maps settings fields to ir.config_parameter keys via config_parameter= attribute.
"""

from odoo import models, fields


class ResConfigSettings(models.TransientModel):
    _inherit = 'res.config.settings'

    # Gemini configuration
    unifix_gemini_api_key = fields.Char(
        string='Gemini API Key',
        config_parameter='unifix.gemini_api_key',
    )
    unifix_gemini_model = fields.Char(
        string='Gemini Model',
        config_parameter='unifix.gemini_model',
        default='gemini-2.5-pro',
    )
    unifix_transcript_prompt = fields.Text(
        string='Transcript Prompt',
        config_parameter='unifix.transcript_prompt',
    )

    # Processing configuration
    unifix_max_video_size_mb = fields.Integer(
        string='Max Video Size (MB)',
        config_parameter='unifix.max_video_size_mb',
        default=1024,
    )
    unifix_temp_dir = fields.Char(
        string='Temp Directory',
        config_parameter='unifix.temp_dir',
    )
    unifix_cleanup_age_hours = fields.Integer(
        string='Cleanup Age (hours)',
        config_parameter='unifix.cleanup_age_hours',
        default=1,
    )

    # Keyframe configuration
    unifix_max_keyframes = fields.Integer(
        string='Max Keyframes',
        config_parameter='unifix.max_keyframes',
        default=12,
    )
    unifix_frame_quality = fields.Integer(
        string='Frame Quality (2=best, 31=worst)',
        config_parameter='unifix.frame_quality',
        default=3,
    )
