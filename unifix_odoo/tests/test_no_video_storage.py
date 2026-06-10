"""Guard tests — Constraint #1: No video in Odoo storage.

Video must never persist as a Binary field or video/* attachment.
Derived images (keyframes) are permitted.
"""

import base64

from odoo.tests.common import TransactionCase
from odoo import fields


class TestNoVideoStorage(TransactionCase):
    """Video must never persist. Derived images (keyframes) are allowed."""

    def test_no_binary_field_on_job_model(self):
        """Job model must not have any Binary fields."""
        job = self.env['unifix.video.job']
        for name, field in job._fields.items():
            self.assertNotIsInstance(
                field, fields.Binary,
                f"Binary field '{name}' on job model — video must not be storable here."
            )

    def test_no_binary_field_on_segment_model(self):
        """Segment model must not have any Binary fields."""
        segment = self.env['unifix.video.segment']
        for name, field in segment._fields.items():
            self.assertNotIsInstance(
                field, fields.Binary,
                f"Binary field '{name}' on segment model."
            )

    def test_no_video_attachment_created(self):
        """No attachment with a video/* mimetype may exist for a job."""
        job = self.env['unifix.video.job'].create({
            'name': 'Test Guard Job',
            'state': 'received',
        })
        vids = self.env['ir.attachment'].search([
            ('res_model', '=', 'unifix.video.job'),
            ('res_id', '=', job.id),
            ('mimetype', '=ilike', 'video/%'),
        ])
        self.assertFalse(
            vids,
            "A video/* attachment exists for a job — violates Constraint #1."
        )

    def test_keyframe_image_attachment_is_allowed(self):
        """An image keyframe is permitted derived data, not a violation."""
        # Minimal valid JPEG (SOI + EOI markers)
        jpeg_data = b'\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00\xff\xd9'
        job = self.env['unifix.video.job'].create({
            'name': 'Test Keyframe Guard',
            'state': 'done',
        })
        kf = self.env['unifix.video.keyframe'].create({
            'job_id': job.id,
            'timestamp': 1.0,
            'image': base64.b64encode(jpeg_data),
        })
        self.assertTrue(kf.image, "Keyframe image should be stored.")

    def test_keyframe_has_image_field_not_binary(self):
        """Keyframe model should use Image field (not raw Binary)."""
        kf_model = self.env['unifix.video.keyframe']
        image_field = kf_model._fields.get('image')
        self.assertIsNotNone(image_field, "Keyframe model must have an 'image' field.")
        # In Odoo, fields.Image is a subclass of fields.Binary,
        # but it's the acceptable form for derived data.
        self.assertIsInstance(
            image_field, fields.Image,
            "Keyframe 'image' field should be fields.Image, not plain Binary."
        )
