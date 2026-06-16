"""Framework-free keyframe selection from a video + cue timestamps.

Gemini supplies cue timestamps — from the AUDIO ("audio_cues"/"verified" modes,
no video tokens) or from watching the video ("gemini_video"). For each cue we
score the frames in a window for a composite of sharpness + exposure + contrast,
then either:
  * keep the single best frame (``extract`` — audio_cues / gemini_video), or
  * return the top-K candidates per cue (``candidates_per_cue`` — verified mode)
    for a downstream Gemini visual-verification pass that picks the frame which
    actually shows the captioned object.
Survivors are de-duplicated (aHash), capped, and JPEG-encoded.

Pure OpenCV — no Odoo dependency, so it is unit-testable against a fixture clip.
"""

import logging

logger = logging.getLogger("work_order_processor")


class KeyframeExtractor:
    def __init__(self, window_before=0.3, window_after=1.5, max_scan=60,
                 dedup_distance=10, min_bright=25, max_bright=235,
                 jpeg_max_dim=1280, jpeg_quality=80):
        self.window_before = window_before
        self.window_after = window_after
        self.max_scan = max_scan
        self.dedup_distance = dedup_distance
        self.min_bright = min_bright
        self.max_bright = max_bright
        self.jpeg_max_dim = jpeg_max_dim
        self.jpeg_quality = jpeg_quality

    # ── Public API ───────────────────────────────────────────────────────────

    def extract(self, video_path, cues, max_kf):
        """One best frame per cue (audio_cues / gemini_video modes).
        cues: [(t, caption, text)] → [(t, caption, text, jpeg_bytes)] chronological."""
        import cv2
        cap = cv2.VideoCapture(video_path)
        if not cap.isOpened():
            logger.warning("Cannot open video for keyframes: %s", video_path)
            return []
        try:
            duration = self._duration(cap)
            picked = []
            for t, caption, text in self._candidates(cues, duration):
                best = self._best_frame(cap, t)
                if best is not None:
                    picked.append((t, caption, text, best))
        finally:
            cap.release()
        return self._dedupe_and_cap(picked, max_kf)

    def candidates_per_cue(self, video_path, cues, k_per_cue=3, max_cues=8):
        """Up to *k_per_cue* deduped candidate frames per cue (verified mode), for
        a downstream visual-verification pass. Returns a list of dicts:
        ``{'t','caption','text','candidates':[(t_frame, jpeg_full, jpeg_thumb)]}``."""
        import cv2
        cap = cv2.VideoCapture(video_path)
        if not cap.isOpened():
            logger.warning("Cannot open video for keyframes: %s", video_path)
            return []
        try:
            duration = self._duration(cap)
            out = []
            for t, caption, text in self._candidates(cues, duration):
                frames = self._window_frames(cap, t)
                frames.sort(key=lambda x: x[2], reverse=True)
                kept, hashes = [], []
                for pos, frame, _q in frames:
                    h = self._ahash(frame)
                    if any(self._hamming(h, hk) < self.dedup_distance for hk in hashes):
                        continue
                    full = self.encode_jpeg(frame)
                    thumb = self.encode_jpeg(frame, max_dim=512, quality=70)
                    if not full or not thumb:
                        continue
                    hashes.append(h)
                    kept.append((pos, full, thumb))
                    if len(kept) >= k_per_cue:
                        break
                if kept:
                    out.append({'t': t, 'caption': caption, 'text': text,
                                'candidates': kept})
                if len(out) >= max_cues:
                    break
            return out
        finally:
            cap.release()

    # ── Frame scoring ─────────────────────────────────────────────────────────

    def _frame_quality(self, gray):
        """Composite quality (higher better); -1 rejects an over/under-exposed frame.

        Sharpness is gated by how central the exposure is and how much contrast
        the frame has, so a crisp but washed-out / blown-out / flat frame ranks
        below one with a clear, well-exposed subject — pure Laplacian variance
        otherwise favours busy backgrounds over the thing being shown.
        """
        import cv2
        bright = float(gray.mean())
        if bright < self.min_bright or bright > self.max_bright:
            return -1.0
        sharp = cv2.Laplacian(gray, cv2.CV_64F).var()
        contrast = float(gray.std())
        exposure = 1.0 - abs(bright - 127.5) / 127.5       # 1 at mid-grey → 0 at extremes
        contrast_factor = min(1.0, contrast / 50.0)
        return sharp * (0.5 + 0.5 * exposure) * (0.5 + 0.5 * contrast_factor)

    def _window_frames(self, cap, t):
        """Acceptable frames in [t-before, t+after] as (pos, frame, quality)."""
        import cv2
        cap.set(cv2.CAP_PROP_POS_MSEC, max(0.0, t - self.window_before) * 1000.0)
        end = t + self.window_after
        out = []
        scanned = 0
        while scanned < self.max_scan:
            pos = cap.get(cv2.CAP_PROP_POS_MSEC) / 1000.0
            ret, frame = cap.read()
            if not ret:
                break
            scanned += 1
            if pos > end:
                break
            gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            q = self._frame_quality(gray)
            if q < 0:
                continue
            out.append((pos, frame.copy(), q))
        return out

    def _best_frame(self, cap, t):
        """Highest-quality frame in the window → (quality, frame)."""
        frames = self._window_frames(cap, t)
        if not frames:
            return None
        _pos, frame, q = max(frames, key=lambda x: x[2])
        return (q, frame)

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _candidates(self, cues, duration):
        """Cue timestamps (primary) + uniform-interval fallback if too sparse."""
        cands = [(t, c, x) for (t, c, x) in cues if 0 <= t <= (duration or t + 1)]
        if len(cands) < 3 and duration and duration > 1:
            for i in range(1, 6):
                cands.append((duration * i / 6.0, "", ""))
        return sorted(cands, key=lambda x: x[0])

    @staticmethod
    def _duration(cap):
        import cv2
        fps = cap.get(cv2.CAP_PROP_FPS) or 25.0
        frames_n = cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0
        return (frames_n / fps) if fps else 0.0

    @staticmethod
    def _ahash(frame):
        import cv2
        g = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        g = cv2.resize(g, (8, 8), interpolation=cv2.INTER_AREA)
        return (g > g.mean()).flatten()

    @staticmethod
    def _hamming(a, b):
        import numpy as np
        return int(np.count_nonzero(a != b))

    def _dedupe_and_cap(self, picked, max_kf):
        # Keep the highest-quality first so duplicates resolve to the best frame.
        hashes, kept = [], []
        for t, caption, text, (q, frame) in sorted(
                picked, key=lambda x: x[3][0], reverse=True):
            h = self._ahash(frame)
            if any(self._hamming(h, hk) < self.dedup_distance for hk in hashes):
                continue
            jpg = self.encode_jpeg(frame)
            if not jpg:
                continue
            hashes.append(h)
            kept.append((t, caption, text, jpg))
            if len(kept) >= max_kf:
                break
        return sorted(kept, key=lambda x: x[0])

    def encode_jpeg(self, frame, max_dim=None, quality=None):
        import cv2
        max_dim = self.jpeg_max_dim if max_dim is None else max_dim
        quality = self.jpeg_quality if quality is None else quality
        h, w = frame.shape[:2]
        scale = min(1.0, max_dim / float(max(h, w) or 1))
        if scale < 1.0:
            frame = cv2.resize(frame, (int(w * scale), int(h * scale)),
                               interpolation=cv2.INTER_AREA)
        ok, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, quality])
        return buf.tobytes() if ok else None
