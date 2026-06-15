{
    "name": "Unifix Work Order Processing",
    "version": "19.0.3.0.0",
    "category": "Services",
    "summary": "AI work-order extraction from audio, image, and video (Gemini)",
    "description": """
        Upload audio, image, or video and extract structured work-order data with
        Gemini (via Vertex AI / ADC). Audio and image media are stored; video is
        processed to audio only and never persisted (keyframe extraction is a
        planned future feature). The extraction engine is vendored under
        processing/ and validates fields against fixtures or live Odoo records.
    """,
    "author": "Unifix",
    "website": "https://unifix.example.com",
    "license": "LGPL-3",
    "depends": ["base", "mail"],
    "external_dependencies": {
        "python": [
            "google.genai",
            "cv2",
            "magic",
            "rapidfuzz",
            "jsonschema",
            "cryptography",
            "yaml",
        ],
    },
    "data": [
        "security/security.xml",
        "security/ir.model.access.csv",
        "data/cron.xml",
        "data/default_params.xml",
        "views/workorder_job_views.xml",
        "views/workorder_job_menu.xml",
        "views/ai_prompt_views.xml",
        "views/res_config_settings_views.xml",
    ],
    "post_init_hook": "post_init_hook",
    "installable": True,
    "auto_install": False,
    "application": True,
}
