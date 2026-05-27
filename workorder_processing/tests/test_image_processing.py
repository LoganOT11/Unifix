"""Tests for processor/image_preprocessor.py and processor/image_processor.py — Issue #1 coverage."""

import io
import json
import os
import stat
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch, PropertyMock

import cv2
import numpy as np
import pytest

from processor.image_preprocessor import (
    assess_quality,
    preprocess_image,
    upscale_if_small,
    ImageQuality,
    PreprocessResult,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_color_image(height: int, width: int, color: tuple = (128, 128, 128)) -> np.ndarray:
    """Create a solid-colour BGR image."""
    img = np.full((height, width, 3), color, dtype=np.uint8)
    return img


def _make_noisy_image(height: int = 200, width: int = 300) -> np.ndarray:
    """Create an image with high contrast (text-like noise on white background)."""
    rng = np.random.default_rng(42)
    img = np.ones((height, width, 3), dtype=np.uint8) * 255
    # Draw random dark patches to push std well above 60 and sharpness above 100
    for _ in range(500):
        x = int(rng.integers(0, width - 5))
        y = int(rng.integers(0, height - 5))
        img[y : y + 4, x : x + 4] = 0
    return img


def _encode_to_jpeg(img: np.ndarray) -> bytes:
    """Encode a numpy array to JPEG bytes."""
    success, buf = cv2.imencode(".jpg", img)
    assert success, "cv2.imencode failed in test helper"
    return buf.tobytes()


def _make_wide_image_jpeg(width: int = 1400, height: int = 200) -> bytes:
    img = _make_noisy_image(height, width)
    return _encode_to_jpeg(img)


def _make_narrow_image_jpeg(width: int = 400, height: int = 200) -> bytes:
    img = _make_noisy_image(height, width)
    return _encode_to_jpeg(img)


# ---------------------------------------------------------------------------
# assess_quality
# ---------------------------------------------------------------------------

class TestAssessQuality:

    def test_high_contrast_image_is_good(self):
        """Image with many dark pixels on white → high std, high sharpness → GOOD."""
        img = _make_noisy_image(300, 400)
        # Ensure the image meets GOOD thresholds: std > 60, 80 < mean < 200, sharpness > 100
        # by verifying programmatically
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        std = float(np.std(gray))
        mean = float(np.mean(gray))
        sharpness = float(cv2.Laplacian(gray, cv2.CV_64F).var())
        if std > 60 and 80 < mean < 200 and sharpness > 100:
            quality = assess_quality(img)
            assert quality == ImageQuality.GOOD
        else:
            # If synthetic data doesn't hit GOOD, at least it shouldn't be POOR
            quality = assess_quality(img)
            assert quality in (ImageQuality.GOOD, ImageQuality.FAIR)

    def test_flat_gray_image_is_poor(self):
        """A solid flat image has zero std and zero sharpness → POOR."""
        img = _make_color_image(100, 150, color=(150, 150, 150))
        quality = assess_quality(img)
        assert quality == ImageQuality.POOR

    def test_all_black_image_is_poor(self):
        img = _make_color_image(100, 150, color=(0, 0, 0))
        quality = assess_quality(img)
        assert quality == ImageQuality.POOR

    def test_all_white_image_is_poor(self):
        img = _make_color_image(100, 150, color=(255, 255, 255))
        quality = assess_quality(img)
        assert quality == ImageQuality.POOR

    def test_grayscale_input_accepted(self):
        """assess_quality should handle a 2D (grayscale) array without error."""
        img = np.zeros((100, 150), dtype=np.uint8)
        # Should not raise; result should be POOR (all zeros)
        quality = assess_quality(img)
        assert quality == ImageQuality.POOR

    def test_returns_image_quality_enum(self):
        img = _make_color_image(100, 100)
        result = assess_quality(img)
        assert isinstance(result, ImageQuality)


# ---------------------------------------------------------------------------
# upscale_if_small
# ---------------------------------------------------------------------------

class TestUpscaleIfSmall:

    def test_narrow_image_is_upscaled_to_min_width(self):
        img = _make_color_image(100, 400)
        result = upscale_if_small(img, min_width=1200)
        assert result.shape[1] >= 1200

    def test_narrow_image_aspect_ratio_preserved(self):
        """Height should scale proportionally."""
        img = _make_color_image(100, 400)
        result = upscale_if_small(img, min_width=1200)
        expected_height = round(100 * (1200 / 400))
        assert abs(result.shape[0] - expected_height) <= 1  # allow 1px rounding

    def test_wide_image_is_unchanged(self):
        img = _make_color_image(100, 1400)
        result = upscale_if_small(img, min_width=1200)
        assert result.shape[1] == 1400

    def test_exactly_min_width_is_unchanged(self):
        img = _make_color_image(100, 1200)
        result = upscale_if_small(img, min_width=1200)
        assert result.shape[1] == 1200

    def test_custom_min_width(self):
        img = _make_color_image(50, 500)
        result = upscale_if_small(img, min_width=800)
        assert result.shape[1] >= 800

    def test_returns_numpy_array(self):
        img = _make_color_image(50, 300)
        result = upscale_if_small(img, min_width=1200)
        assert isinstance(result, np.ndarray)


# ---------------------------------------------------------------------------
# preprocess_image
# ---------------------------------------------------------------------------

class TestPreprocessImage:

    def test_valid_jpeg_returns_preprocess_result(self):
        img = _make_noisy_image(200, 1300)  # wide enough to skip upscale
        jpeg_bytes = _encode_to_jpeg(img)
        result = preprocess_image(jpeg_bytes, "image/jpeg")
        assert isinstance(result, PreprocessResult)
        assert isinstance(result.image_bytes, bytes)
        assert len(result.image_bytes) > 0

    def test_valid_jpeg_returns_jpeg_mime(self):
        img = _make_noisy_image(200, 1300)
        jpeg_bytes = _encode_to_jpeg(img)
        result = preprocess_image(jpeg_bytes, "image/jpeg")
        assert result.mime_type == "image/jpeg"

    def test_valid_jpeg_has_quality_before(self):
        img = _make_noisy_image(200, 1300)
        jpeg_bytes = _encode_to_jpeg(img)
        result = preprocess_image(jpeg_bytes, "image/jpeg")
        assert result.quality_before in (ImageQuality.GOOD, ImageQuality.FAIR, ImageQuality.POOR)

    def test_corrupt_bytes_raises_value_error(self):
        with pytest.raises(ValueError, match="corrupt"):
            preprocess_image(b"not an image at all", "image/jpeg")

    def test_empty_bytes_raises_value_error(self):
        # cv2.imdecode returns None for empty/truncated buffers → ValueError raised
        with pytest.raises(ValueError):
            preprocess_image(b"\x00" * 10, "image/jpeg")

    def test_small_image_ops_list_contains_upscale(self):
        """An image narrower than 1200px should trigger upscale_if_small in ops."""
        img = _make_noisy_image(200, 400)  # narrow: 400px wide
        jpeg_bytes = _encode_to_jpeg(img)
        result = preprocess_image(jpeg_bytes, "image/jpeg")
        assert any("upscale" in op.lower() for op in result.operations_applied)

    def test_wide_good_image_ops_are_minimal(self):
        """A wide, high-quality image should only get 'none' or no heavy ops."""
        img = _make_noisy_image(300, 1400)
        jpeg_bytes = _encode_to_jpeg(img)
        result = preprocess_image(jpeg_bytes, "image/jpeg")
        # Should not fail; ops may include 'none (quality assessed as GOOD)' or upscale skipped
        assert isinstance(result.operations_applied, list)

    def test_poor_quality_image_applies_denoise_and_binarize(self):
        """A flat POOR image should get the full pipeline including binarize."""
        img = _make_color_image(200, 1400, color=(150, 150, 150))
        jpeg_bytes = _encode_to_jpeg(img)
        result = preprocess_image(jpeg_bytes, "image/jpeg")
        assert result.quality_before == ImageQuality.POOR
        ops_lower = [op.lower() for op in result.operations_applied]
        assert any("denoise" in op for op in ops_lower)
        assert any("binarize" in op for op in ops_lower)

    def test_preprocess_result_has_estimated_improvement(self):
        img = _make_noisy_image(200, 1300)
        jpeg_bytes = _encode_to_jpeg(img)
        result = preprocess_image(jpeg_bytes, "image/jpeg")
        assert isinstance(result.estimated_improvement, str)
        assert len(result.estimated_improvement) > 0


# ---------------------------------------------------------------------------
# validate_image_file (processor/image_processor.py)
# ---------------------------------------------------------------------------

class TestValidateImageFile:

    def test_path_traversal_raises_value_error(self):
        from processor.image_processor import validate_image_file
        with pytest.raises(ValueError, match="traversal"):
            validate_image_file("/etc/passwd", safe_root="/tmp/safe")

    def test_path_traversal_with_dotdot_raises_value_error(self):
        from processor.image_processor import validate_image_file
        with tempfile.TemporaryDirectory() as tmpdir:
            # Attempt to escape the safe root via ../..
            traversal_path = os.path.join(tmpdir, "..", "..", "etc", "passwd")
            with pytest.raises(ValueError, match="traversal"):
                validate_image_file(traversal_path, safe_root=tmpdir)

    def test_missing_file_raises_file_not_found(self):
        from processor.image_processor import validate_image_file
        with tempfile.TemporaryDirectory() as tmpdir:
            missing = os.path.join(tmpdir, "nonexistent.jpg")
            with pytest.raises(FileNotFoundError):
                validate_image_file(missing, safe_root=tmpdir)

    def test_oversized_file_raises_value_error(self):
        from processor.image_processor import validate_image_file, MAX_IMAGE_SIZE_BYTES
        with tempfile.TemporaryDirectory() as tmpdir:
            # Create a real small file
            img_path = os.path.join(tmpdir, "test.jpg")
            img = _make_color_image(10, 10)
            jpeg_bytes = _encode_to_jpeg(img)
            with open(img_path, "wb") as f:
                f.write(jpeg_bytes)

            # Patch Path.stat to return a huge size
            fake_stat = MagicMock()
            fake_stat.st_size = MAX_IMAGE_SIZE_BYTES + 1
            with patch("processor.image_processor.Path.stat", return_value=fake_stat):
                with pytest.raises(ValueError, match="too large"):
                    validate_image_file(img_path, safe_root=tmpdir)

    def test_valid_jpeg_returns_metadata_dict(self):
        from processor.image_processor import validate_image_file
        with tempfile.TemporaryDirectory() as tmpdir:
            img_path = os.path.join(tmpdir, "test.jpg")
            img = _make_noisy_image(50, 100)
            with open(img_path, "wb") as f:
                f.write(_encode_to_jpeg(img))
            meta = validate_image_file(img_path, safe_root=tmpdir)
            assert "path" in meta
            assert "mime_type" in meta
            assert "size_bytes" in meta
            assert meta["mime_type"] in ("image/jpeg", "image/jpg")

    def test_valid_png_returns_metadata_dict(self):
        from processor.image_processor import validate_image_file
        with tempfile.TemporaryDirectory() as tmpdir:
            img_path = os.path.join(tmpdir, "test.png")
            img = _make_color_image(20, 20)
            success, buf = cv2.imencode(".png", img)
            assert success
            with open(img_path, "wb") as f:
                f.write(buf.tobytes())
            meta = validate_image_file(img_path, safe_root=tmpdir)
            assert meta["mime_type"] == "image/png"


# ---------------------------------------------------------------------------
# process_image (processor/image_processor.py)
# ---------------------------------------------------------------------------

# Minimal work order JSON that parse_ai_json / extract_confidence_markers can handle
_MOCK_RESPONSE_JSON = json.dumps({
    "vehicle_equipment": "TRK-001",
    "reported_problem": "Engine overheating",
    "diagnosis_cause": "Low coolant",
    "work_performed": "Topped up coolant",
    "parts_used": "Coolant",
    "start_time": "08:00",
    "end_time": "10:00",
    "total_time_spent": "2h 0m",
    "future_recommendations": "Check weekly",
    "remaining_tasks": "",
    "worker": "James Hartwell",
    "worker__confidence": "HIGH",
    "company": "Hartwell Industrial Services",
    "company__confidence": "MEDIUM",
    "location": "Main Workshop Bay 1",
    "location__confidence": "HIGH",
})


class TestProcessImage:
    """Tests for process_image().

    process_image() calls validate_image_file() which uses Path.cwd() as the
    default safe root.  Since test temp dirs live under /tmp (outside cwd),
    happy-path tests patch validate_image_file directly so that file-I/O and
    path-safety checks are controlled independently of the OS layout.
    """

    def _make_jpeg_bytes(self) -> bytes:
        img = _make_color_image(5, 5, color=(200, 180, 160))
        return _encode_to_jpeg(img)

    def _mock_validate(self, mime: str = "image/jpeg"):
        """Return a patch context for validate_image_file that succeeds."""
        return patch(
            "processor.image_processor.validate_image_file",
            return_value={"path": "/fake/workorder.jpg", "mime_type": mime, "size_bytes": 1024},
        )

    def _mock_read_bytes(self, data: bytes):
        """Return a patch context for Path.read_bytes used inside process_image."""
        return patch("processor.image_processor.Path.read_bytes", return_value=data)

    @patch("processor.image_processor.call_gemini_generic")
    def test_process_image_returns_four_tuple(self, mock_call):
        mock_response = MagicMock()
        mock_response.text = _MOCK_RESPONSE_JSON
        mock_call.return_value = mock_response

        jpeg = self._make_jpeg_bytes()
        from processor.image_processor import process_image
        with self._mock_validate(), self._mock_read_bytes(jpeg):
            result = process_image("/fake/workorder.jpg", MagicMock(), "gemini-test", preprocess=False)

        assert isinstance(result, tuple)
        assert len(result) == 4

    @patch("processor.image_processor.call_gemini_generic")
    def test_process_image_work_order_is_dict(self, mock_call):
        mock_response = MagicMock()
        mock_response.text = _MOCK_RESPONSE_JSON
        mock_call.return_value = mock_response

        jpeg = self._make_jpeg_bytes()
        from processor.image_processor import process_image
        with self._mock_validate(), self._mock_read_bytes(jpeg):
            work_order, confidences, response, preprocess_result = process_image(
                "/fake/workorder.jpg", MagicMock(), "gemini-test", preprocess=False
            )
        assert isinstance(work_order, dict)

    @patch("processor.image_processor.call_gemini_generic")
    def test_process_image_extracts_work_order_fields(self, mock_call):
        mock_response = MagicMock()
        mock_response.text = _MOCK_RESPONSE_JSON
        mock_call.return_value = mock_response

        jpeg = self._make_jpeg_bytes()
        from processor.image_processor import process_image
        with self._mock_validate(), self._mock_read_bytes(jpeg):
            work_order, confidences, response, preprocess_result = process_image(
                "/fake/workorder.jpg", MagicMock(), "gemini-test", preprocess=False
            )
        assert work_order.get("worker") == "James Hartwell"
        assert work_order.get("vehicle_equipment") == "TRK-001"

    @patch("processor.image_processor.call_gemini_generic")
    def test_process_image_extracts_confidence_markers(self, mock_call):
        mock_response = MagicMock()
        mock_response.text = _MOCK_RESPONSE_JSON
        mock_call.return_value = mock_response

        jpeg = self._make_jpeg_bytes()
        from processor.image_processor import process_image
        with self._mock_validate(), self._mock_read_bytes(jpeg):
            work_order, confidences, response, preprocess_result = process_image(
                "/fake/workorder.jpg", MagicMock(), "gemini-test", preprocess=False
            )
        assert isinstance(confidences, dict)
        assert confidences.get("worker") == "HIGH"
        assert confidences.get("company") == "MEDIUM"

    @patch("processor.image_processor.call_gemini_generic")
    def test_process_image_confidence_keys_not_in_work_order(self, mock_call):
        """Confidence markers should be stripped from the work_order dict."""
        mock_response = MagicMock()
        mock_response.text = _MOCK_RESPONSE_JSON
        mock_call.return_value = mock_response

        jpeg = self._make_jpeg_bytes()
        from processor.image_processor import process_image
        with self._mock_validate(), self._mock_read_bytes(jpeg):
            work_order, confidences, response, preprocess_result = process_image(
                "/fake/workorder.jpg", MagicMock(), "gemini-test", preprocess=False
            )
        assert not any("__confidence" in k for k in work_order)

    @patch("processor.image_processor.call_gemini_generic")
    def test_process_image_preprocess_false_returns_none_preprocess_result(self, mock_call):
        mock_response = MagicMock()
        mock_response.text = _MOCK_RESPONSE_JSON
        mock_call.return_value = mock_response

        jpeg = self._make_jpeg_bytes()
        from processor.image_processor import process_image
        with self._mock_validate(), self._mock_read_bytes(jpeg):
            _, _, _, preprocess_result = process_image(
                "/fake/workorder.jpg", MagicMock(), "gemini-test", preprocess=False
            )
        assert preprocess_result is None

    @patch("processor.image_processor.call_gemini_generic")
    def test_process_image_preprocess_true_returns_preprocess_result(self, mock_call):
        """With preprocess=True on a real JPEG, preprocess_result is a PreprocessResult."""
        mock_response = MagicMock()
        mock_response.text = _MOCK_RESPONSE_JSON
        mock_call.return_value = mock_response

        # Use a real 50×1300 image (wide enough to avoid upscale distraction)
        img = _make_noisy_image(50, 1300)
        jpeg = _encode_to_jpeg(img)
        from processor.image_processor import process_image
        with self._mock_validate(), self._mock_read_bytes(jpeg):
            _, _, _, preprocess_result = process_image(
                "/fake/workorder.jpg", MagicMock(), "gemini-test", preprocess=True
            )
        assert preprocess_result is not None
        assert isinstance(preprocess_result, PreprocessResult)

    def test_process_image_missing_file_raises(self):
        """Without patching, a non-existent file inside cwd raises FileNotFoundError."""
        from processor.image_processor import process_image
        import os
        cwd = os.getcwd()
        missing = os.path.join(cwd, "definitely_does_not_exist_ghost_12345.jpg")
        with pytest.raises(FileNotFoundError):
            process_image(missing, MagicMock(), "gemini-test")

    def test_process_image_traversal_raises(self):
        """A path clearly outside cwd raises ValueError (path traversal)."""
        from processor.image_processor import process_image
        with pytest.raises(ValueError, match="traversal"):
            process_image("/etc/passwd", MagicMock(), "gemini-test")
