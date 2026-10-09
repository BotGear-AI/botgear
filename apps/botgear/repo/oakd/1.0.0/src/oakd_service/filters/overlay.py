"""Draw the frame number or a wall-clock timestamp onto each frame.

Pure effect — no telemetry. Useful for correlating a recorded/streamed
clip against logs or other telemetry after the fact.
"""
from __future__ import annotations

import time
from typing import Any, Dict, Optional

import cv2
import numpy as np

from .base import Filter, make_param


class OverlayFilter(Filter):
    type_name = "overlay"
    title = "Overlay"
    description = (
        "Draws the frame number or a timestamp in a corner of the frame. "
        "The frame counter starts at 0 when the filter is added (added/removed "
        "state resets it — it isn't the camera's lifetime frame count)."
    )
    param_schema = [
        make_param(
            "mode", "enum", "timestamp",
            choices=["timestamp", "frame_number"],
            label="Mode",
            help="What to draw: a wall-clock timestamp, or a running frame counter.",
        ),
        make_param(
            "position", "enum", "bottom_left",
            choices=["top_left", "top_right", "bottom_left", "bottom_right"],
            label="Position",
            help="Corner of the frame to draw in.",
        ),
        make_param(
            "font_scale", "float", 0.6, min=0.3, max=2.0, step=0.1,
            label="Font size",
        ),
    ]

    _MARGIN = 10
    _FONT = cv2.FONT_HERSHEY_SIMPLEX

    def __init__(self, id: str, params: Optional[Dict[str, Any]] = None) -> None:
        super().__init__(id, params)
        self._frame_no = 0

    def process(self, frame: np.ndarray) -> np.ndarray:
        mode = str(self.params.get("mode") or "timestamp")
        text = f"frame {self._frame_no}" if mode == "frame_number" else time.strftime("%Y-%m-%d %H:%M:%S")
        self._frame_no += 1

        scale = float(self.params.get("font_scale") or 0.6)
        thickness = max(1, round(scale * 2))
        (tw, th), baseline = cv2.getTextSize(text, self._FONT, scale, thickness)

        h, w = frame.shape[:2]
        position = str(self.params.get("position") or "bottom_left")
        x = self._MARGIN if "left" in position else w - tw - self._MARGIN
        y = (th + self._MARGIN) if "top" in position else h - self._MARGIN - baseline

        # Black outline + white fill so the text reads against any
        # background, without needing a configurable color param.
        cv2.putText(frame, text, (x, y), self._FONT, scale, (0, 0, 0), thickness + 2, cv2.LINE_AA)
        cv2.putText(frame, text, (x, y), self._FONT, scale, (255, 255, 255), thickness, cv2.LINE_AA)
        return frame
