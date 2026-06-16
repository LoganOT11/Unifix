"""Image preprocessing for handwritten work order photos before Gemini Vision ingestion."""

import cv2
import numpy as np
from enum import Enum
from dataclasses import dataclass, field


class ImageQuality(str, Enum):
    GOOD = "GOOD"
    FAIR = "FAIR"
    POOR = "POOR"


@dataclass
class PreprocessResult:
    image_bytes: bytes
    mime_type: str
    quality_before: ImageQuality
    operations_applied: list[str] = field(default_factory=list)
    estimated_improvement: str = ""


def assess_quality(img: np.ndarray) -> ImageQuality:
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY) if len(img.shape) == 3 else img
    std = float(np.std(gray))
    mean = float(np.mean(gray))
    sharpness = float(cv2.Laplacian(gray, cv2.CV_64F).var())
    if std > 60 and 80 < mean < 200 and sharpness > 100:
        return ImageQuality.GOOD
    elif std > 35 and sharpness > 40:
        return ImageQuality.FAIR
    return ImageQuality.POOR


def deskew(img: np.ndarray) -> tuple[np.ndarray, float]:
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    edges = cv2.Canny(gray, 50, 150, apertureSize=3)
    lines = cv2.HoughLinesP(edges, 1, np.pi / 180, threshold=100,
                            minLineLength=100, maxLineGap=10)
    if lines is None:
        return img, 0.0
    angles = []
    for line in lines:
        x1, y1, x2, y2 = line[0]
        if x2 != x1:
            angle = np.degrees(np.arctan2(y2 - y1, x2 - x1))
            if abs(angle) < 45:
                angles.append(angle)
    if not angles:
        return img, 0.0
    median_angle = float(np.median(angles))
    if abs(median_angle) < 0.5:
        return img, median_angle
    h, w = img.shape[:2]
    M = cv2.getRotationMatrix2D((w // 2, h // 2), median_angle, 1.0)
    corrected = cv2.warpAffine(img, M, (w, h),
                               flags=cv2.INTER_CUBIC,
                               borderMode=cv2.BORDER_REPLICATE)
    return corrected, median_angle


def enhance_contrast(img: np.ndarray) -> np.ndarray:
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))
    enhanced = clahe.apply(gray)
    return cv2.cvtColor(enhanced, cv2.COLOR_GRAY2BGR)


def denoise(img: np.ndarray) -> np.ndarray:
    return cv2.fastNlMeansDenoisingColored(img, None, h=10, hColor=10,
                                           templateWindowSize=7,
                                           searchWindowSize=21)


def binarize(img: np.ndarray) -> np.ndarray:
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    binary = cv2.adaptiveThreshold(gray, 255,
                                   cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
                                   cv2.THRESH_BINARY, blockSize=11, C=2)
    return cv2.cvtColor(binary, cv2.COLOR_GRAY2BGR)


def upscale_if_small(img: np.ndarray, min_width: int = 1200) -> np.ndarray:
    h, w = img.shape[:2]
    if w < min_width:
        scale = min_width / w
        img = cv2.resize(img, (int(w * scale), int(h * scale)),
                         interpolation=cv2.INTER_LANCZOS4)
    return img


def preprocess_image(image_bytes: bytes, mime_type: str) -> PreprocessResult:
    """Full preprocessing pipeline: quality assessment → selective enhancement → JPEG encode."""
    nparr = np.frombuffer(image_bytes, np.uint8)
    img = cv2.imdecode(nparr, cv2.IMREAD_COLOR)
    if img is None:
        raise ValueError("Could not decode image bytes — file may be corrupt.")

    quality = assess_quality(img)
    ops: list[str] = []

    width_before = img.shape[1]
    img = upscale_if_small(img)
    if img.shape[1] != width_before:
        ops.append(f"upscale_if_small ({width_before}→{img.shape[1]}px wide)")

    if quality in (ImageQuality.FAIR, ImageQuality.POOR):
        img, angle = deskew(img)
        if abs(angle) > 0.5:
            ops.append(f"deskew ({angle:+.1f}°)")
        img = denoise(img)
        ops.append("denoise")
        img = enhance_contrast(img)
        ops.append("enhance_contrast (CLAHE)")

    if quality == ImageQuality.POOR:
        img = binarize(img)
        ops.append("binarize (adaptive threshold)")

    ok, buffer = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 92])
    if not ok or buffer is None:
        raise ValueError("cv2.imencode failed — could not encode the processed image as JPEG.")

    improvement = {
        ImageQuality.GOOD: "Minimal — image was already clean",
        ImageQuality.FAIR: "Moderate — deskew and denoising applied",
        ImageQuality.POOR: "Significant — full enhancement pipeline applied; manual review recommended",
    }[quality]

    return PreprocessResult(
        image_bytes=buffer.tobytes(),
        mime_type="image/jpeg",
        quality_before=quality,
        operations_applied=ops if ops else ["none (quality assessed as GOOD)"],
        estimated_improvement=improvement,
    )
