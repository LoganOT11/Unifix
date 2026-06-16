"""Unit tests for the framework-free media services (compression, keyframes).

These run in the engine's pytest suite (conftest puts processing/ on sys.path)
and need no Odoo and no real media — they synthesise images/frames.
"""

import os
import tempfile

import cv2
import numpy as np

from processor.compression import compress_image
from processor.keyframes import KeyframeExtractor


def _gradient_image(w, h):
    x = np.linspace(0, 255, w, dtype="uint8")
    img = np.repeat(x[None, :], h, axis=0)
    return cv2.cvtColor(img, cv2.COLOR_GRAY2BGR)


def _write_png(w, h):
    fd, path = tempfile.mkstemp(suffix=".png")
    os.close(fd)
    cv2.imwrite(path, _gradient_image(w, h))
    return path


# ── compression.compress_image ───────────────────────────────────────────────

def test_compress_image_caps_dimension_and_shrinks():
    path = _write_png(4000, 3000)
    try:
        data, ext = compress_image(path, max_dim=2048, quality=80)
        assert ext == ".jpg" and data
        arr = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR)
        assert max(arr.shape[:2]) <= 2048
        assert len(data) < 4000 * 3000 * 3          # far smaller than raw pixels
    finally:
        os.unlink(path)


def test_compress_image_no_upscale_small_image():
    path = _write_png(800, 600)
    try:
        data, _ = compress_image(path, max_dim=2048)
        arr = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR)
        assert (arr.shape[1], arr.shape[0]) == (800, 600)   # unchanged
    finally:
        os.unlink(path)


def test_compress_image_unsupported_returns_none():
    fd, path = tempfile.mkstemp(suffix=".pdf")
    os.write(fd, b"%PDF-1.4 not an image")
    os.close(fd)
    try:
        assert compress_image(path) == (None, None)
    finally:
        os.unlink(path)


# ── KeyframeExtractor (pure logic, no video file) ────────────────────────────

def test_encode_jpeg_caps_dimension():
    frame = _gradient_image(3000, 2000)
    jpg = KeyframeExtractor(jpeg_max_dim=1280).encode_jpeg(frame)
    assert jpg
    arr = cv2.imdecode(np.frombuffer(jpg, np.uint8), cv2.IMREAD_COLOR)
    assert max(arr.shape[:2]) <= 1280


def test_candidates_interval_fallback_when_sparse():
    kf = KeyframeExtractor()
    cands = kf._candidates([(1.0, "a", "x")], duration=60.0)
    assert len(cands) >= 5                       # interval fallback kicked in
    assert cands == sorted(cands, key=lambda c: c[0])


def test_candidates_drop_out_of_range():
    kf = KeyframeExtractor()
    cands = kf._candidates([(5.0, "a", "x"), (999.0, "b", "y")], duration=10.0)
    assert all(0 <= t <= 10.0 for t, _, _ in cands)


def test_frame_quality_rejects_over_and_underexposed():
    kf = KeyframeExtractor(min_bright=25, max_bright=235)
    dark = np.full((48, 64), 5, dtype="uint8")
    blown = np.full((48, 64), 250, dtype="uint8")
    assert kf._frame_quality(dark) < 0
    assert kf._frame_quality(blown) < 0


def test_frame_quality_positive_for_textured_midtone():
    kf = KeyframeExtractor()
    g = (np.random.rand(48, 64) * 255).astype("uint8")   # exposed + contrast + detail
    assert kf._frame_quality(g) > 0


def test_encode_jpeg_thumbnail_override():
    frame = _gradient_image(2000, 1500)
    thumb = KeyframeExtractor().encode_jpeg(frame, max_dim=512, quality=70)
    assert thumb
    arr = cv2.imdecode(np.frombuffer(thumb, np.uint8), cv2.IMREAD_COLOR)
    assert max(arr.shape[:2]) <= 512


def test_dedupe_keeps_sharpest_and_dedups_identical():
    kf = KeyframeExtractor(dedup_distance=10)
    frame = _gradient_image(640, 480)
    # two identical frames (different sharpness scores) + one distinct
    distinct = cv2.cvtColor((np.random.rand(480, 640) * 255).astype("uint8"),
                            cv2.COLOR_GRAY2BGR)
    picked = [
        (1.0, "a", "ta", (10.0, frame.copy())),
        (2.0, "b", "tb", (99.0, frame.copy())),     # identical to first, sharper
        (3.0, "c", "tc", (50.0, distinct)),
    ]
    kept = kf._dedupe_and_cap(picked, max_kf=8)
    # the two identical frames collapse to one (the sharper, t=2.0); distinct stays
    times = [t for t, *_ in kept]
    assert 2.0 in times and 3.0 in times and 1.0 not in times
    assert len(kept) == 2
