"""
AURORA-HSI

Open-source hyperspectral imaging system for quantitative optical characterization
of biological, chemical, and material samples.

Institution:
  Colorado School of Mines
  Chemical & Biological Engineering
  CASH Lab

File: camera.py
License: MIT (see LICENSE file in project root)
Copyright (c) 2026 Dr. Kevin Cash
"""
import os
import time
from typing import Optional

import numpy as np
import zwoasi as asi


class Camera:
    def __init__(self, sdk_library_path: str | None):
        lib = sdk_library_path or os.getenv("ZWO_ASI_LIB")
        if not lib:
            raise RuntimeError("SDK library filename required (arg or ZWO_ASI_LIB).")
        asi.init(lib)
        if asi.get_num_cameras() == 0:
            raise RuntimeError("No ZWO ASI cameras found.")
        self.cam = asi.Camera(0)
        try:
            prop = self.cam.get_camera_property()
            self.max_w = int(prop.get('MaxWidth', 0))
            self.max_h = int(prop.get('MaxHeight', 0))
        except Exception:
            self.max_w = 0
            self.max_h = 0
        self._prime()
    
    def _prime(self):
        # defaults from your script
        self.cam.disable_dark_subtract()
        ctl = self.cam.get_controls()
        try:
            self.cam.set_control_value(asi.ASI_BANDWIDTHOVERLOAD, ctl['BandWidth']['MinValue'])
        except Exception:
            pass
        self.cam.set_control_value(asi.ASI_GAIN, 150)
        self.cam.set_control_value(asi.ASI_EXPOSURE, 30000)
        # White balance is irrelevant for mono sensors; keep neutral defaults if present.
        try:
            self.cam.set_control_value(asi.ASI_WB_B, 99)
            self.cam.set_control_value(asi.ASI_WB_R, 75)
        except Exception:
            pass
        # Display/preview controls are best kept at neutral for raw workflows.
        try:
            self.cam.set_control_value(asi.ASI_GAMMA, 0)
        except Exception:
            pass
        try:
            self.cam.set_control_value(asi.ASI_BRIGHTNESS, 0)
        except Exception:
            pass
        self.cam.set_control_value(asi.ASI_FLIP, 0)

        # Prefer RAW16 container for max preservation (camera ADC is typically <= 16-bit).
        try:
            self.cam.set_image_type(asi.ASI_IMG_RAW16)
        except Exception:
            pass
        # Enforce full-frame ROI to avoid partial-frame captures.
        self._force_full_roi_raw16()
        try:
            self.cam.stop_video_capture()
            self.cam.stop_exposure()
        except Exception:
            pass

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()

    def close(self):
        try:
            # Some drivers require explicit stop before delete
            self.stop_capture()
        finally:
            # Let GC clean up; asi library doesn’t need explicit shutdown
            self.cam = None

    def set_params(self, exposure_us: int, gain: int):
        if self.cam is None:
            raise RuntimeError("Camera is closed; cannot set params.")
        self.cam.set_control_value(asi.ASI_EXPOSURE, int(exposure_us))
        self.cam.set_control_value(asi.ASI_GAIN, int(gain))
        # Re-assert full-frame ROI here so set_params() is the single recovery
        # point after a failure, instead of doing it redundantly on every frame.
        self._force_full_roi_raw16()

    def get_camera_name(self) -> str:
        """Return a human-friendly camera name (best-effort)."""
        try:
            p = self.cam.get_camera_property()
            return str(p.get("Name") or p.get("CameraName") or p.get("name") or "ZWO ASI")
        except Exception:
            return "ZWO ASI"

    def get_control_value_safe(self, control_id: int) -> Optional[int]:
        """Best-effort read of a control value (returns int or None).
        
        The zwoasi library returns either a (value, auto) tuple or a
        [value, auto] list depending on the SDK version. Handle both.
        """
        try:
            v = self.cam.get_control_value(control_id)
            # zwoasi may return (value, auto_flag) as tuple or list
            if isinstance(v, (tuple, list)):
                return int(v[0])
            return int(v)
        except Exception:
            return None


    def _force_full_roi_raw16(self):
        """Best-effort: ensure full-frame ROI at bin=1 and RAW16 image type.

        If ROI start/size was previously changed (or SDK default isn't full frame),
        enforcing this prevents 'partial frame' captures.
        """
        try:
            self.cam.set_image_type(asi.ASI_IMG_RAW16)
        except Exception:
            pass

        # Use cached max dimensions when available; fall back to properties.
        max_w = getattr(self, "max_w", 0) or 0
        max_h = getattr(self, "max_h", 0) or 0
        if not (max_w and max_h):
            try:
                prop = self.cam.get_camera_property()
                max_w = int(prop.get("MaxWidth", 0))
                max_h = int(prop.get("MaxHeight", 0))
            except Exception:
                max_w = max_h = 0

        # Force ROI to full frame, bin=1.
        if max_w and max_h:
            try:
                # zwoasi expects: width, height, bin, image_type
                self.cam.set_roi_format(max_w, max_h, 1, asi.ASI_IMG_RAW16)
            except Exception:
                pass
            try:
                # Ensure start position is at origin
                self.cam.set_start_pos(0, 0)
            except Exception:
                pass

    def _get_roi_wh(self) -> tuple[int, int]:
        """Return (width, height) for current ROI (best-effort)."""
        try:
            # zwoasi: (width, height, bin, image_type)
            w, h, *_ = self.cam.get_roi_format()
            return int(w), int(h)
        except Exception:
            try:
                p = self.cam.get_camera_property()
                return int(p.get('MaxWidth', 0)), int(p.get('MaxHeight', 0))
            except Exception:
                return (0, 0)

    def capture_png(self, filepath: str) -> str:
        """Quick-look capture using the SDK filename helper."""
        self.cam.capture(filename=filepath)
        return filepath

    def stop_capture(self):
        """Stop any ongoing exposure; safe to call from error-recovery paths."""
        try:
            self.cam.stop_exposure()
        except Exception:
            pass
        try:
            self.cam.stop_video_capture()
        except Exception:
            pass

    def capture_raw16(self, timeout_s: float = 10.0, is_dark: bool = False) -> np.ndarray:
        """Capture a single frame in RAW16 mode and return a uint16 (H, W) array.

        Notes:
        - Uses start_exposure/get_data_after_exposure for broad compatibility.
        - The ASI533MM Pro has a ~14-bit ADC; RAW16 is the standard container.
        """
        # Use cached full-frame dimensions (set_params asserts full-frame ROI).
        # Fall back to a live query only if the cache is missing.
        if self.max_w and self.max_h:
            w, h = self.max_w, self.max_h
        else:
            w, h = self._get_roi_wh()

        # Start exposure and poll
        try:
            self.cam.start_exposure(bool(is_dark))
        except TypeError:
            self.cam.start_exposure(is_dark)

        t0 = time.time()
        try:
            while True:
                status = self.cam.get_exposure_status()
                now = time.time()
                if status == asi.ASI_EXP_SUCCESS:
                    break
                if status == asi.ASI_EXP_FAILED:
                    raise RuntimeError("Camera exposure failed (ASI_EXP_FAILED).")
                if (time.time() - t0) > timeout_s:
                    raise TimeoutError(f"Camera exposure timed out after {timeout_s:.1f}s")
                time.sleep(0.005)
        except Exception:
            # Always stop the hardware exposure before propagating; otherwise the
            # next start_exposure() call races a still-pending exposure in the SDK.
            self.stop_capture()
            raise

        data = self.cam.get_data_after_exposure()

        # Normalize to uint16 image
        if isinstance(data, np.ndarray):
            if data.dtype == np.uint16:
                frame = data
                if frame.ndim == 1 and w and h:
                    frame = frame.reshape((h, w))
                return np.ascontiguousarray(frame)
            buf = data.tobytes()
        else:
            buf = bytes(data)

        # ASI SDK returns little-endian RAW16 in practice; store as native uint16 array.
        arr = np.frombuffer(buf, dtype="<u2")
        if w and h and arr.size >= (w * h):
            arr = arr[: (w * h)].reshape((h, w))
        return np.ascontiguousarray(arr)
    
    def cooler(self, on: bool, target_c: int | None = None):
        self.cam.set_control_value(asi.ASI_COOLER_ON, 1 if on else 0)
        if on and target_c is not None:
            self.cam.set_control_value(asi.ASI_TARGET_TEMP, int(target_c))