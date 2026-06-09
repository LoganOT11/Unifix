{
    "name": "Unifix Video Processing",
    "version": "19.0.1.0.0",
    "category": "Services",
    "summary": "Ephemeral video processing pipeline with Gemini AI",
    "description": """
        Upload video files for AI-powered transcription and work-order extraction.
        The video is never persisted in Odoo storage — only derived data
        (transcript, segments, keyframe images, and extracted fields) is stored.
    """,
    "author": "Unifix",
    "website": "https://unifix.example.com",
    "license": "LGPL-3",
    "depends": ["base"],
    "data": [
        "security/security.xml",
        "security/ir.model.access.csv",
        "data/cron.xml",
        "data/default_params.xml",
        "views/video_job_views.xml",
        "views/video_job_menu.xml",
        "views/res_config_settings_views.xml",
    ],
    "installable": True,
    "auto_install": False,
    "application": True,
}
