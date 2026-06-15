"""Configuration settings for Unifix Video Processing.

Maps settings fields to ir.config_parameter keys via config_parameter= attribute.
"""

from odoo import models, fields


class ResConfigSettings(models.TransientModel):
    _inherit = 'res.config.settings'

    # Gemini auth
    unifix_use_vertexai = fields.Boolean(
        string='Use Vertex AI (ADC)',
        config_parameter='unifix.use_vertexai',
        help="Authenticate via Vertex AI + Application Default Credentials "
             "(no API key). Project/location come from GOOGLE_CLOUD_PROJECT / "
             "GOOGLE_CLOUD_LOCATION unless set below.",
    )
    unifix_gcp_project = fields.Char(
        string='GCP Project',
        config_parameter='unifix.gcp_project',
    )
    unifix_gcp_location = fields.Char(
        string='GCP Location',
        config_parameter='unifix.gcp_location',
    )
    unifix_gemini_api_key = fields.Char(
        string='Gemini API Key',
        config_parameter='unifix.gemini_api_key',
        help="Only used when Vertex AI is disabled.",
    )
    unifix_gemini_model = fields.Char(
        string='Gemini Model',
        config_parameter='unifix.gemini_model',
        default='gemini-3.1-flash-lite',
    )

    # Field validation
    unifix_reference_provider = fields.Selection(
        [('memory', 'Bundled fixtures'), ('odoo', 'Live Odoo records')],
        string='Reference Data Source',
        config_parameter='unifix.reference_provider',
        default='memory',
    )

    # Processing configuration
    unifix_max_video_size_mb = fields.Integer(
        string='Max Video Size (MB)',
        config_parameter='unifix.max_video_size_mb',
        default=1024,
    )
    unifix_max_audio_size_mb = fields.Integer(
        string='Max Audio Size (MB)',
        config_parameter='unifix.max_audio_size_mb',
        default=200,
    )
    unifix_max_image_size_mb = fields.Integer(
        string='Max Image Size (MB)',
        config_parameter='unifix.max_image_size_mb',
        default=25,
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
