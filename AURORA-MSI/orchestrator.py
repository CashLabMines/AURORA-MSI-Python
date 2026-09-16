"""
AURORA-HSI

Open-source hyperspectral imaging system for quantitative optical characterization
of biological, chemical, and material samples.

Institution:
  Colorado School of Mines
  Chemical & Biological Engineering
  CASH Lab

File: orchestrator.py
License: MIT (see LICENSE file in project root)
Copyright (c) 2026 Dr. Kevin Cash
"""
from __future__ import annotations

import datetime
import os
import re
import shutil
import threading
import time
from typing import Any, Callable

import numpy as np

from config import Config

try:
    import tifffile  # type: ignore
except ImportError:
    tifffile = None



def _require_tifffile() -> None:
    if tifffile is None:
        raise RuntimeError(
            "tifffile is required to write 16-bit TIFF with embedded metadata. "
            "Install with: pip install tifffile"
        )


def _scale_asi_temp(raw: Any) -> float | None:
    """Best-effort scaling for ASI temperature control values.

    Many ZWO ASI SDK builds report temperatures in 0.1°C units.
    To stay robust across SDK variants, we treat |raw|>100 as 'tenths of °C'.
    zwoasi may return a list [value, auto_flag] instead of a plain value.
    """
    try:
        if raw is None:
            return None
        # Handle list/tuple return from zwoasi get_control_value
        if isinstance(raw, (list, tuple)):
            raw = raw[0]
        v = float(raw)
        if abs(v) > 100:
            v = v / 10.0
        return v
    except Exception:
        return None


def _thermal_header_from_camera(camera: Any) -> dict[str, Any]:
    """Collect cooler + temperature fields from the camera (best-effort)."""
    out: dict[str, Any] = {}
    try:
        import zwoasi as asi  # local import keeps orchestrator decoupled

        getv = getattr(camera, "get_control_value_safe", None)
        if not callable(getv):
            return out

        # Measured sensor temperature (often 0.1°C units)
        t_raw = None
        try:
            t_raw = getv(asi.ASI_TEMPERATURE)
        except Exception:
            t_raw = None
        t_c = _scale_asi_temp(t_raw)
        if t_c is not None:
            out["CCD_TEMP_C"] = t_c

        # Cooler on/off
        cool_on = None
        try:
            cool_on = getv(asi.ASI_COOLER_ON)
        except Exception:
            cool_on = None
        if cool_on is not None:
            out["COOLER_ON"] = bool(int(cool_on))

        # Target temperature
        tgt_raw = None
        try:
            tgt_raw = getv(asi.ASI_TARGET_TEMP)
        except Exception:
            tgt_raw = None
        tgt_c = _scale_asi_temp(tgt_raw)
        if tgt_c is not None:
            out["TARGET_TEMP_C"] = tgt_c

        # Cooler power percentage (if supported)
        try:
            pow_raw = getv(getattr(asi, "ASI_COOLER_POWER_PERC"))
            if pow_raw is not None:
                out["COOLER_POWER_PCT"] = float(pow_raw)
        except Exception:
            pass

    except Exception:
        # If zwoasi isn't available or a control isn't supported, just omit.
        return out

    return out


def _slug(s: str) -> str:
    return re.sub(r"[^0-9A-Za-z_.-]+", "_", s).strip("_")


def _ascii_safe(s: str) -> str:
    """Replace common non-ASCII characters so TIFF ASCII (type 2) tags write cleanly."""
    s = s.replace("µ", "u").replace("°", "deg").replace("≤", "<=").replace("≥", ">=")
    return s.encode("ascii", errors="replace").decode("ascii")


def _ascii_tag(s: str) -> tuple[bytes, int]:
    """Return (null-terminated ASCII bytes, byte count) for a TIFF ASCII extratag."""
    b = _ascii_safe(s).encode("ascii") + b"\x00"
    return b, len(b)


def _capture_with_timeout(camera, timeout_s: float = 30.0, **kwargs) -> np.ndarray:
    """Run camera.capture_raw16() in a daemon sub-thread with a hard wall-clock timeout.

    Prints a 1-second heartbeat while waiting so the log shows exactly when the
    SDK hangs.  Raises TimeoutError if no frame arrives within timeout_s seconds.
    """
    _result: list[np.ndarray] = []
    _exc: list[BaseException] = []

    def _do() -> None:
        try:
            _result.append(camera.capture_raw16(**kwargs))
        except Exception as e:
            _exc.append(e)

    t = threading.Thread(target=_do, daemon=True, name="CameraCapture")
    t.start()

    t0 = time.time()
    while t.is_alive():
        now = time.time()
        elapsed = now - t0
        if elapsed >= timeout_s:
            raise TimeoutError(
                f"camera.capture_raw16() did not return within {timeout_s:.0f}s"
            )
        t.join(timeout=0.1)

    if _exc:
        raise _exc[0]
    return _result[0]


def _build_xmp(metadata: dict[str, Any]) -> bytes:
    """Encode metadata as an XMP/RDF XML block embedded in a TIFF XMP tag (700).

    All key/value pairs are written as dc:description properties under a single
    rdf:Description element.  Values are serialised to strings; lists become
    comma-separated strings.  This is readable by ExifTool, FIJI/ImageJ, and
    most scientific image viewers.
    """
    def _to_str(v: Any) -> str:
        if isinstance(v, list):
            return ", ".join(str(x) for x in v)
        return str(v)

    props = "\n    ".join(
        f'<aurora:{k}>{_to_str(v)}</aurora:{k}>'
        for k, v in sorted(metadata.items())
    )

    xmp = (
        '<?xpacket begin="\xef\xbb\xbf" id="W5M0MpCehiHzreSzNTczkc9d"?>\n'
        '<x:xmpmeta xmlns:x="adobe:ns:meta/">\n'
        ' <rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">\n'
        '  <rdf:Description rdf:about=""\n'
        '   xmlns:aurora="https://github.com/AuroraHSI/schema/1.0/">\n'
        f'    {props}\n'
        '  </rdf:Description>\n'
        ' </rdf:RDF>\n'
        '</x:xmpmeta>\n'
        '<?xpacket end="w"?>'
    )
    return xmp.encode("utf-8")


def _encode_xp(text: str) -> bytes:
    """Encode a string as UTF-16LE bytes for Windows XP-era TIFF tags (40091–40095).

    Windows reads these tags as null-terminated UTF-16LE character arrays.
    """
    return (text + "\x00").encode("utf-16-le")


def _build_exif_ifd(file_offset: int, exposure_us=None, gain=None, comment=None, dt_str=None) -> bytes:
    """Build a minimal EXIF IFD blob with value offsets absolute to the file.

    file_offset is where this blob will be appended in the file, so internal
    value pointers are calculated as file_offset + ifd_header_size + ...
    """
    import struct
    entries = []
    SHORT, RATIONAL, UNDEFINED, ASCII = 3, 5, 7, 2

    if exposure_us is not None:
        entries.append((33434, RATIONAL, 1, struct.pack("<II", int(exposure_us), 1_000_000)))
    if gain is not None:
        entries.append((34855, SHORT, 1, struct.pack("<H", int(gain))))
    if dt_str:
        data = dt_str.encode("ascii") + b"\x00"
        entries.append((36868, ASCII, len(data), data))
    if comment:
        uc = b"UNICODE\x00" + comment.encode("utf-16-le")
        entries.append((37510, UNDEFINED, len(uc), uc))

    entries.sort(key=lambda e: e[0])
    n = len(entries)
    ifd_body_size = 2 + n * 12 + 4
    value_offset = file_offset + ifd_body_size

    ifd_bytes = struct.pack("<H", n)
    value_data = b""

    for tag, typ, count, data in entries:
        if len(data) <= 4:
            padded = data + b"\x00" * (4 - len(data))
            ifd_bytes += struct.pack("<HHI", tag, typ, count) + padded
        else:
            ifd_bytes += struct.pack("<HHI", tag, typ, count) + struct.pack("<I", value_offset)
            value_data += data
            value_offset += len(data)

    return ifd_bytes + struct.pack("<I", 0) + value_data


def _inject_exif_ifd(tiff_path: str, exposure_us=None, gain=None, comment=None, dt_str=None) -> None:
    """Post-process a written TIFF file to inject a proper EXIF IFD (tag 34665).

    tifffile blocks writing tag 34665 directly via extratags, so we patch the
    file afterward.  The EXIF IFD is appended to the end of the file and a new
    IFD (with tag 34665 added) replaces the original IFD via the TIFF header
    pointer.  The original image data is untouched.

    Uses seek-based in-place patching so only the header + IFD (~hundreds of
    bytes) are read into RAM, not the full image.  The old approach loaded the
    entire file into a bytearray, which for large sensors consumed 2× the file
    size per frame (e.g. 2×120 MB) and caused monotonically growing RSS.
    """
    import struct
    try:
        with open(tiff_path, "r+b") as f:
            # Read TIFF header (8 bytes) for byte-order and IFD offset
            header = f.read(8)
            endian = "<" if header[:2] == b"II" else ">"
            ifd_offset = struct.unpack_from(endian + "I", header, 4)[0]

            # Read the IFD entry count and all entries (2 + n*12 + 4 bytes)
            f.seek(ifd_offset)
            n_entries = struct.unpack_from(endian + "H", f.read(2))[0]
            ifd_raw = f.read(n_entries * 12 + 4)  # entries + next-IFD pointer

            # Skip if tag 34665 already present
            for i in range(n_entries):
                tag = struct.unpack_from(endian + "H", ifd_raw, i * 12)[0]
                if tag == 34665:
                    return

            # Append EXIF blob at end of file
            f.seek(0, 2)
            exif_offset = f.tell()
            exif_blob = _build_exif_ifd(exif_offset, exposure_us, gain, comment, dt_str)
            f.write(exif_blob)

            # Build new IFD that includes tag 34665
            existing = [bytes(ifd_raw[i * 12:(i + 1) * 12]) for i in range(n_entries)]
            new_entry = struct.pack(endian + "HHII", 34665, 4, 1, exif_offset)
            insert_pos = next((i for i, e in enumerate(existing)
                               if struct.unpack_from(endian + "H", e, 0)[0] > 34665), len(existing))
            existing.insert(insert_pos, new_entry)
            next_ifd = struct.unpack_from(endian + "I", ifd_raw, n_entries * 12)[0]

            # Append new IFD at end of file
            new_ifd_offset = f.tell()
            f.write(struct.pack(endian + "H", len(existing)))
            for entry in existing:
                f.write(entry)
            f.write(struct.pack(endian + "I", next_ifd))

            # Patch the TIFF header to point at the new IFD (4 bytes at offset 4)
            f.seek(4)
            f.write(struct.pack(endian + "I", new_ifd_offset))
    except Exception:
        pass  # EXIF injection is best-effort; don't break the file


def save_tiff_u16(path: str, frame_u16: np.ndarray, metadata: dict[str, Any] | None = None) -> str:
    """Write a 2D uint16 TIFF with metadata in standard TIFF + EXIF tags.

    Tags written:
      270  ImageDescription  — human-readable summary (all viewers)
      305  Software          — experiment name
      306  DateTime          — capture timestamp (TIFF format)
      315  Artist            — camera name
      700  XMP               — full structured metadata block (ExifTool, FIJI)

    Windows Details panel (right-click → Properties → Details):
      33434  ExposureTime    — exposure in seconds (shown as fraction)
      34855  ISOSpeedRatings — gain value (mapped to ISO field)
      36868  DateTimeDigitized — capture timestamp
      37510  UserComment     — comments field
      40091  XPTitle         — experiment name
      40094  XPKeywords      — LED wavelengths + enables as semicolon list
      40095  XPSubject       — wavelength(s) selected
    """
    _require_tifffile()

    if frame_u16.ndim != 2:
        raise ValueError("TIFF writer expects a 2D image array")
    if frame_u16.dtype != np.uint16:
        frame_u16 = np.asarray(frame_u16, dtype=np.uint16)

    out_dir = os.path.dirname(path)
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)

    extratags: list[tuple] = []
    _description: str | None = None
    _software: str | None = None

    if metadata:
        def _liststr(key: str) -> str:
            v = metadata.get(key, [])
            if isinstance(v, list):
                return ", ".join(str(x) for x in v)
            return str(v) if v else ""

        # --- Tag 270: ImageDescription — compact human-readable summary ---
        summary_keys = (
            "WAVELENGTH", "LED_LABELS", "INTENSITY_PCT", "EXPOSURE_US", "GAIN",
            "DATE_OBS", "EXPERIMENT", "ENABLE", "EN_LABELS",
            "CCD_TEMP_C", "TARGET_TEMP_C", "FILTER_LABELS", "COMMENTS",
        )
        summary_parts = []
        for k in summary_keys:
            if k not in metadata:
                continue
            v = metadata[k]
            if isinstance(v, list):
                summary_parts.append(f"{k}={', '.join(str(x) for x in v)}")
            else:
                summary_parts.append(f"{k}={v}")
        if not summary_parts:
            summary_parts = [f"{k}={v}" for k, v in sorted(metadata.items())]
        _description = _ascii_safe("; ".join(summary_parts))

        # --- Tag 305: Software — experiment name ---
        experiment = metadata.get("EXPERIMENT")
        _software = _ascii_safe(str(experiment)) if experiment else None

        # --- Tag 306: DateTime — "YYYY:MM:DD HH:MM:SS" ---
        date_obs = metadata.get("DATE_OBS")
        tiff_dt = ""
        if date_obs:
            try:
                dt_str = str(date_obs).replace("T", " ").split(".")[0]
                date_part, time_part = dt_str.split(" ")
                tiff_dt = date_part.replace("-", ":") + " " + time_part
                _b, _n = _ascii_tag(tiff_dt)
                extratags.append((306, 2, _n, _b, True))
            except Exception:
                pass

        # --- Tag 315: Artist — camera name ---
        camera_name = metadata.get("CAMERA")
        if camera_name:
            _b, _n = _ascii_tag(str(camera_name))
            extratags.append((315, 2, _n, _b, True))

        # --- Tag 700: XMP — full structured metadata block ---
        xmp_bytes = _build_xmp(metadata)
        extratags.append((700, 1, len(xmp_bytes), xmp_bytes, False))

        # ---- Windows Details panel EXIF tags ----

        # Tag 33434: ExposureTime — rational (numerator, denominator)
        exp_us = metadata.get("EXPOSURE_US")
        if exp_us is not None:
            try:
                # Store as microseconds/1000000 = seconds rational
                extratags.append((33434, 5, 1, (int(exp_us), 1_000_000), True))
            except Exception:
                pass

        # Tag 34855: ISOSpeedRatings — gain (SHORT array)
        gain = metadata.get("GAIN")
        if gain is not None:
            try:
                extratags.append((34855, 3, 1, int(gain), True))
            except Exception:
                pass

        # Tag 36868: DateTimeDigitized — same as DateTime
        if tiff_dt:
            _b, _n = _ascii_tag(tiff_dt)
            extratags.append((36868, 2, _n, _b, True))

        # Tag 40092: XPComment — the field Windows Details "Comments" actually reads.
        # Tag 37510: UserComment — EXIF standard comments (also written for broader compat).
        # Build a rich auto-generated comment string from all available metadata fields,
        # then append any user-supplied comment text at the end.
        def _fmt_list(key):
            v = metadata.get(key, [])
            if isinstance(v, list):
                return ", ".join(str(x) for x in v) if v else ""
            return str(v) if v else ""

        comment_parts = []

        exp = metadata.get("EXPERIMENT")
        if exp:
            comment_parts.append(f"Experiment: {exp}")

        leds = _fmt_list("LED_LABELS") or str(metadata.get("WAVELENGTH", ""))
        if leds:
            comment_parts.append(f"Wavelengths: {leds}")

        ens = _fmt_list("EN_LABELS")
        if ens:
            comment_parts.append(f"Enables: {ens}")

        intensity = metadata.get("INTENSITY_PCT")
        if intensity is not None:
            comment_parts.append(f"Intensity: {float(intensity):.0f}%")

        exp_us = metadata.get("EXPOSURE_US")
        if exp_us is not None:
            ms = float(exp_us) / 1000.0
            if ms < 1.0:
                comment_parts.append(f"Exposure: {int(exp_us)} µs")
            elif ms < 1000.0:
                comment_parts.append(f"Exposure: {ms:.3f} ms")
            else:
                comment_parts.append(f"Exposure: {ms/1000.0:.3f} s")

        gain = metadata.get("GAIN")
        if gain is not None:
            comment_parts.append(f"Gain: {gain}")

        ccd_temp = metadata.get("CCD_TEMP_C")
        if ccd_temp is not None:
            comment_parts.append(f"Sensor Temp: {float(ccd_temp):.1f} °C")

        target_temp = metadata.get("TARGET_TEMP_C")
        if target_temp is not None:
            comment_parts.append(f"Target Temp: {float(target_temp):.1f} °C")

        cooler_on = metadata.get("COOLER_ON")
        if cooler_on is not None:
            comment_parts.append(f"Cooler: {'ON' if cooler_on else 'OFF'}")

        cooler_pwr = metadata.get("COOLER_POWER_PCT")
        if cooler_pwr is not None:
            comment_parts.append(f"Cooler Power: {float(cooler_pwr):.0f}%")

        cam = metadata.get("CAMERA")
        if cam:
            comment_parts.append(f"Camera: {cam}")

        date_obs = metadata.get("DATE_OBS")
        if date_obs:
            comment_parts.append(f"Captured: {date_obs}")

        shape = metadata.get("SHAPE_HW")
        if shape and isinstance(shape, list) and len(shape) == 2:
            comment_parts.append(f"Size: {shape[1]}x{shape[0]} px")

        # Append user comment last if present
        user_comment = metadata.get("COMMENTS", "").strip()
        if user_comment:
            comment_parts.append(f"Notes: {user_comment}")

        comments = " | ".join(comment_parts)

        if comments:
            try:
                b = _encode_xp(comments)
                extratags.append((40092, 1, len(b), b, True))
            except Exception:
                pass
            try:
                charset = b"UNICODE\x00"
                uc_bytes = charset + comments.encode("utf-16-le")
                extratags.append((37510, 7, len(uc_bytes), uc_bytes, True))
            except Exception:
                pass

        # Tag 40091: XPTitle — experiment name (UTF-16LE)
        if experiment:
            try:
                b = _encode_xp(str(experiment))
                extratags.append((40091, 1, len(b), b, True))
            except Exception:
                pass

        # Tag 40094: XPKeywords — LEDs + enables as semicolon-separated (Windows convention)
        led_str = _liststr("LED_LABELS")
        en_str = _liststr("EN_LABELS")
        kw_parts = [p for p in [led_str, en_str] if p]
        if kw_parts:
            try:
                b = _encode_xp(";".join(kw_parts))
                extratags.append((40094, 1, len(b), b, True))
            except Exception:
                pass

        # Tag 40095: XPSubject — wavelength(s) selected
        subject = _liststr("LED_LABELS") or metadata.get("WAVELENGTH", "")
        if subject:
            try:
                b = _encode_xp(str(subject))
                extratags.append((40095, 1, len(b), b, True))
            except Exception:
                pass

    tifffile.imwrite(
        path,
        frame_u16,
        dtype=np.uint16,
        photometric="minisblack",
        description=_description if metadata else None,
        software=_software if metadata else None,
        extratags=extratags if extratags else None,
    )

    # Inject proper EXIF IFD so Windows Details panel shows Exposure, ISO, Comments etc.
    if metadata:
        _inject_exif_ifd(
            path,
            exposure_us=metadata.get("EXPOSURE_US"),
            gain=metadata.get("GAIN"),
            comment=metadata.get("COMMENTS"),
            dt_str=tiff_dt if tiff_dt else None,
        )

    return path


class Orchestrator:
    def __init__(self, pico, camera, led_map: dict[str, int], en_map: dict[str, int]):
        self.pico = pico
        self.camera = camera
        self.led_map = led_map
        self.en_map = en_map

    # -------- paths / filenames --------
    def _ensure_dir(self, experiment: str) -> str:
        out_dir = os.path.join(Config.BASE_IMAGE_DIR, experiment)
        os.makedirs(out_dir, exist_ok=True)
        return out_dir

    def _check_no_overwrite(self, path: str) -> None:
        """Raise an error if a file already exists at the given path."""
        if os.path.exists(path):
            raise FileExistsError(
                f"A file named '{os.path.basename(path)}' already exists in this folder. "
                f"Please use a different experiment name."
            )

    def build_filename(self, experiment: str) -> str:
        return os.path.join(self._ensure_dir(experiment), f"{experiment}.tiff")

    def build_metadata_filename(
        self,
        experiment: str,
        wavelength: str,
        intensity_pct: int,
        enable_tag: str,
        exposure_us: int,
        gain: int = 0,
    ) -> str:
        parts = [
            experiment,
            _slug(wavelength),
            f"{int(intensity_pct)}pct",
            f"{exposure_us}us",
            f"gain{gain}",
        ]
        fname = "_".join(parts) + ".tiff"
        return os.path.join(self._ensure_dir(experiment), fname)

    def build_burst_filename(self, experiment: str, shot_idx: int, shot_total: int) -> str:
        ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S_%f")[:-3]
        fname = f"{experiment}_{ts}_shot{shot_idx:04d}of{shot_total:04d}.tiff"
        return os.path.join(self._ensure_dir(experiment), fname)

    # -------- device helpers --------
    def apply_enables(self, selected_en_labels: list[str]):
        all_en_pins = list(self.en_map.values())
        on_pins = [self.en_map[lbl] for lbl in selected_en_labels if lbl in self.en_map]
        self.pico.set_enables(all_en_pins, on_pins)

    def set_led_intensity(self, selected_led_labels: list[str], intensity_pct: float, freq_hz: int | None = None):
        freq = freq_hz if freq_hz is not None else Config.PWM_FREQ
        duty = max(0.0, min(1.0, (float(intensity_pct) / 100.0)))
        if selected_led_labels:
            sel_pins = [self.led_map[lbl] for lbl in selected_led_labels if lbl in self.led_map]
            self.pico.all_leds_off(self.led_map.values(), freq)
            if sel_pins:
                self.pico.pwm(sel_pins, duty, freq)
        else:
            self.pico.all_leds_off(self.led_map.values(), freq)

    def all_off(self):
        self.pico.all_leds_off(self.led_map.values(), Config.PWM_FREQ)
        self.pico.all_enables_low(self.en_map.values())

    # -------- internal metadata helpers --------
    def _base_metadata(
        self,
        experiment: str,
        exposure_us: int,
        gain: int,
        selected_led_labels: list[str],
        selected_en_labels: list[str],
        intensity_pct: float,
        comments: str = "",
    ) -> dict[str, Any]:
        meta: dict[str, Any] = {
            "DATE_OBS": datetime.datetime.now().strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3],
            "EXPTIME_S": float(exposure_us) / 1e6,
            "EXPOSURE_US": int(exposure_us),
            "GAIN": int(gain),
            "LED_LABELS": selected_led_labels if selected_led_labels else [],
            "EN_LABELS": selected_en_labels if selected_en_labels else [],
            "INTENSITY_PCT": float(intensity_pct),
            "EXPERIMENT": str(experiment),
            "CAMERA": getattr(self.camera, "get_camera_name", lambda: "ZWO ASI")(),
            "DTYPE": "uint16",
        }
        if comments:
            meta["COMMENTS"] = str(comments)
        meta.update(_thermal_header_from_camera(self.camera))
        return meta

    def _save_frame_tiff(
        self,
        path: str,
        frame: np.ndarray,
        meta: dict[str, Any],
        shot_idx: int | None = None,
        shot_total: int | None = None,
    ) -> str:
        meta = dict(meta)
        if shot_idx is not None:
            meta["SHOT_IDX"] = int(shot_idx)
        if shot_total is not None:
            meta["SHOT_TOTAL"] = int(shot_total)
        meta["SHAPE_HW"] = [int(frame.shape[0]), int(frame.shape[1])]
        return save_tiff_u16(path, frame, meta)

    # -------- one-shot capture --------
    def capture_frame(
        self,
        experiment: str,
        exposure_us: int,
        gain: int,
        selected_led_labels: list[str],
        selected_en_labels: list[str],
        intensity_pct: float,
        settle_s: float = 0.05,
        comments: str = "",
        folder_override: str | None = None,
    ):
        if not experiment or not experiment.strip():
            raise ValueError("Please enter an experiment name before capturing an image.")

        _require_tifffile()
        self.apply_enables(selected_en_labels)
        self.set_led_intensity(selected_led_labels, intensity_pct, Config.PWM_FREQ)
        time.sleep(settle_s)

        self.camera.set_params(exposure_us, gain)
        frame = _capture_with_timeout(self.camera)

        folder = folder_override if folder_override else experiment
        path = os.path.join(self._ensure_dir(folder), f"{experiment}.tiff")
        self._check_no_overwrite(path)
        meta = self._base_metadata(experiment, exposure_us, gain, selected_led_labels, selected_en_labels, intensity_pct, comments)
        self._save_frame_tiff(path, frame, meta)
        return path

    # -------- burst capture (same settings, N frames) --------
    def capture_burst(
        self,
        experiment: str,
        exposure_us: int,
        gain: int,
        selected_led_labels: list[str],
        selected_en_labels: list[str],
        intensity_pct: float,
        count: int,
        settle_s: float = 0.05,
        inter_frame_delay_s: float = 1.0,
        progress_cb: Callable[[int, int, str], None] | None = None,
        should_stop: Callable[[], bool] | None = None,
        comments: str = "",
    ) -> list[str]:
        """Capture `count` images back-to-back using the exact same settings."""
        if not experiment or not experiment.strip():
            raise ValueError("Please enter an experiment name before capturing an image.")
        _require_tifffile()
        if count <= 0:
            raise ValueError("count must be >= 1")

        # Do the slow setup once
        self.apply_enables(selected_en_labels)
        self.set_led_intensity(selected_led_labels, intensity_pct, Config.PWM_FREQ)
        time.sleep(settle_s)

        # Set camera once
        self.camera.set_params(exposure_us, gain)

        base_meta = self._base_metadata(experiment, exposure_us, gain, selected_led_labels, selected_en_labels, intensity_pct, comments)

        paths: list[str] = []
        for i in range(count):
            if should_stop and should_stop():
                break

            frame = _capture_with_timeout(self.camera)
            path = self.build_burst_filename(experiment, i + 1, count)
            self._check_no_overwrite(path)
            saved = self._save_frame_tiff(path, frame, base_meta, shot_idx=i + 1, shot_total=count)
            paths.append(saved)

            if progress_cb:
                progress_cb(i + 1, count, f"Burst {i + 1}/{count}")

            if inter_frame_delay_s > 0:
                time.sleep(inter_frame_delay_s)

        return paths

    # -------- enable groups --------
    def _enable_groups(self) -> list[tuple[str, list[str]]]:
        labels = list(self.en_map.keys())
        return [("all", labels)]

    def compute_total_images(
        self,
        selected_led_labels: list[str] | None = None,
        selected_en_labels: list[str] | None = None,
        intensities: list[int] | None = None,
        exposures_us: list[int] | None = None,
        exposures_per_wl: dict[str, int] | None = None,
        intensities_per_wl: dict[str, int] | None = None,
        exposures_per_wl_multi: dict[str, list[int]] | None = None,
        intensities_per_wl_multi: dict[str, list[int]] | None = None,
        gains: list[int] | None = None,
        include_dark: bool = False,
    ) -> int:
        if selected_led_labels:
            wavelengths = [wl for wl in selected_led_labels if wl in self.led_map]
        else:
            wavelengths = list(self.led_map.keys())

        intensities = intensities if intensities else [100]
        gain_list = gains if gains else [0]
        per_wl_mode = bool(exposures_per_wl)
        exposures = exposures_us if exposures_us else [30000]

        if selected_en_labels is not None:
            n_enables = 1 if any(e in self.en_map for e in selected_en_labels) else 0
        else:
            n_enables = len(self._enable_groups())

        total = 0
        for wl in wavelengths:
            if exposures_per_wl_multi and wl in exposures_per_wl_multi:
                n_exp = len(exposures_per_wl_multi[wl] or [30000])
            elif per_wl_mode:
                n_exp = 1
            else:
                n_exp = len(exposures)

            if intensities_per_wl_multi and wl in intensities_per_wl_multi:
                n_int = len(intensities_per_wl_multi[wl] or [100])
            elif intensities_per_wl:
                n_int = 1
            else:
                n_int = len(intensities)

            total += n_exp * n_int * n_enables * len(gain_list)

        if include_dark:
            _all_exps = exposures_us if exposures_us else (
                list(exposures_per_wl.values()) if exposures_per_wl else [30000]
            )
            n_dark = len(list(dict.fromkeys(_all_exps)))
            total += n_dark * len(gain_list)

        return total

    # -------- automatic sweep engine with CSV logging --------
    def auto_loop(
        self,
        experiment: str,
        gain: int,
        settle_s: float,
        progress_cb: Callable[..., None] | None = None,
        should_stop: Callable[[], bool] | None = None,
        selected_led_labels: list[str] | None = None,
        selected_en_labels: list[str] | None = None,
        intensities: list[int] | None = None,
        exposures_us: list[int] | None = None,
        exposures_per_wl: dict[str, int] | None = None,
        intensities_per_wl: dict[str, int] | None = None,
        gains: list[int] | None = None,
        comments: str = "",
        include_dark: bool = False,
        exposures_per_wl_multi: dict[str, list[int]] | None = None,
        intensities_per_wl_multi: dict[str, list[int]] | None = None,
        gains_per_wl_multi: dict[str, list[int]] | None = None,
    ):
        """Nested sweep (TIFF output) with optional CSV logging.

        intensities      — list of intensity percentages to sweep (e.g. [25, 50, 75, 100])
        exposures_us     — list of exposure values in µs swept for every wavelength
        exposures_per_wl — dict of {wavelength_label: exposure_us} for one exposure
                           per wavelength. When provided, exposures_us is ignored.
        intensities_per_wl — dict of {wavelength_label: intensity_pct} for one intensity
                           per wavelength. When provided, intensities list is ignored.
        gains            — list of gain values to sweep (e.g. [0, 50, 100, 150]).
                           Defaults to [single fixed gain] if not provided.
        Both exposure args default to a single 30ms exposure if neither is provided.
        """
        _require_tifffile()

        if selected_led_labels:
            wavelengths = [wl for wl in selected_led_labels if wl in self.led_map]
        else:
            wavelengths = list(self.led_map.keys())

        if not wavelengths:
            raise ValueError("Please select at least one wavelength before collecting photos.")

        intensities = intensities if intensities else [100]
        gain_list = gains if gains else [gain]

        # Per-wavelength mode: one exposure (and optionally intensity) per wavelength
        per_wl_mode = bool(exposures_per_wl)

        # Enables: if UI provides selection, use it (one group).
        if selected_en_labels is not None:
            selected = [e for e in selected_en_labels if e in self.en_map]
            if not selected:
                raise ValueError("Please select at least one enable before collecting photos.")
            enable_groups = [("selected", selected)]
        else:
            enable_groups = self._enable_groups()

        exposures = exposures_us if exposures_us else [30000]
        total = 0
        for wl in wavelengths:
            if exposures_per_wl_multi and wl in exposures_per_wl_multi:
                n_exp = len(exposures_per_wl_multi[wl] or [30000])
            elif per_wl_mode:
                n_exp = 1
            else:
                n_exp = len(exposures)

            if intensities_per_wl_multi and wl in intensities_per_wl_multi:
                n_int = len(intensities_per_wl_multi[wl] or [100])
            elif intensities_per_wl:
                n_int = 1
            else:
                n_int = len(intensities)

            total += n_exp * n_int * len(enable_groups) * len(gain_list)

        # Dark frames: one per unique exposure value per gain, captured before science frames.
        # Deduplicate while preserving order so filenames are predictable.
        if exposures_us:
            _all_exps = exposures_us
        elif exposures_per_wl_multi:
            _all_exps = [e for exps in exposures_per_wl_multi.values() for e in exps]
        elif exposures_per_wl:
            _all_exps = list(exposures_per_wl.values())
        else:
            _all_exps = [30000]
        dark_exposures = list(dict.fromkeys(_all_exps))
        if include_dark:
            total += len(dark_exposures) * len(gain_list)
        done = 0
        # Stop the sweep if this many frames fail back-to-back (camera not recovering).
        _MAX_CONSECUTIVE_FAILS = 5
        _consecutive_fails = 0

        self.all_off()
        try:
            # ---- Dark frames first (LEDs and enables all off) ----
            if include_dark:
                for gain_val in gain_list:
                    for exp_us in dark_exposures:
                        if should_stop and should_stop():
                            self.all_off()
                            if progress_cb:
                                progress_cb(done, total, "Stopped by user.", None)
                            return
                        self.camera.set_params(exp_us, gain_val)
                        fpath = self.build_metadata_filename(
                            experiment=experiment,
                            wavelength="dark",
                            intensity_pct=0,
                            enable_tag="none",
                            exposure_us=exp_us,
                            gain=gain_val,
                        )
                        frame = _capture_with_timeout(self.camera, is_dark=True)
                        meta = {
                            "WAVELENGTH": "dark",
                            "INTENSITY_PCT": 0,
                            "ENABLE": "none",
                            "DARK_FRAME": True,
                        }
                        meta.update(self._base_metadata(
                            experiment, exp_us, gain_val, [], [], 0, comments
                        ))
                        self._check_no_overwrite(fpath)
                        self._save_frame_tiff(fpath, frame, meta)
                        done += 1
                        _dark_extra = {
                            "sensor_temp_c": meta.get("CCD_TEMP_C", ""),
                            "target_temp_c": meta.get("TARGET_TEMP_C", ""),
                            "filters": "",
                        }
                        if progress_cb:
                            progress_cb(done, total, f"Dark | {exp_us}µs | gain {gain_val}", fpath, _dark_extra)

            for wl in wavelengths:
                # Resolve exposures for this wavelength
                if exposures_per_wl_multi and wl in exposures_per_wl_multi:
                    wl_exposures = exposures_per_wl_multi[wl] or [30000]
                elif per_wl_mode:
                    wl_exposures = [exposures_per_wl.get(wl, 30000)] if exposures_per_wl else [30000]
                else:
                    wl_exposures = exposures

                # Resolve intensities for this wavelength
                if intensities_per_wl_multi and wl in intensities_per_wl_multi:
                    wl_intensities = intensities_per_wl_multi[wl] or [100]
                elif intensities_per_wl:
                    wl_intensities = [intensities_per_wl.get(wl, 100)]
                else:
                    wl_intensities = intensities

                # Resolve gains for this wavelength
                if gains_per_wl_multi and wl in gains_per_wl_multi:
                    wl_gains = gains_per_wl_multi[wl] or gain_list
                else:
                    wl_gains = gain_list

                for intensity in wl_intensities:
                    for group_name, group_labels in enable_groups:
                        self.apply_enables(group_labels)
                        time.sleep(settle_s)
                        if group_name in ("odd", "even", "all", "selected"):
                            enable_tag = group_name
                        else:
                            enable_tag = group_labels[0] if group_labels else "none"
                        for exp_us in wl_exposures:
                            # Re-assert LED before every exposure block so a dropped
                            # serial command can't leave the LED in the wrong state.
                            self.set_led_intensity([wl], intensity, Config.PWM_FREQ)
                            time.sleep(settle_s)
                            for gain in wl_gains:
                                if should_stop and should_stop():
                                    self.all_off()
                                    if progress_cb:
                                        progress_cb(done, total, "Stopped by user.", None)
                                    return

                                self.camera.set_params(exp_us, gain)
                                fpath = self.build_metadata_filename(
                                    experiment=experiment,
                                    wavelength=wl,
                                    intensity_pct=intensity,
                                    enable_tag=enable_tag,
                                    exposure_us=exp_us,
                                    gain=gain,
                                )
                                try:
                                    try:
                                        _df_free = shutil.disk_usage(os.path.dirname(fpath) or ".").free / 1e9
                                        if _df_free < 0.5:
                                            raise RuntimeError(
                                                f"Disk almost full ({_df_free:.2f} GB free) — sweep aborted to prevent data corruption."
                                            )
                                    except RuntimeError:
                                        raise
                                    except Exception:
                                        pass

                                    frame = _capture_with_timeout(self.camera)

                                    meta = self._base_metadata(experiment, exp_us, gain, [wl], group_labels, intensity, comments)
                                    meta.update({
                                        "WAVELENGTH": str(wl),
                                        "INTENSITY_PCT": int(intensity),
                                        "ENABLE": str(enable_tag),
                                    })
                                    # Add filter labels to TIFF metadata
                                    meta["FILTER_LABELS"] = getattr(self, "_current_filter_labels", [])

                                    self._check_no_overwrite(fpath)
                                    self._save_frame_tiff(fpath, frame, meta)

                                    # Build extra_meta for CSV logger (real capture-time values)
                                    _extra = {
                                        "sensor_temp_c": meta.get("CCD_TEMP_C", ""),
                                        "target_temp_c": meta.get("TARGET_TEMP_C", ""),
                                        "filters": ";".join(meta.get("FILTER_LABELS", [])),
                                        "exposure_us": exp_us,
                                        "intensity_pct": intensity,
                                        "led_wavelength": str(wl),
                                        "gain": gain,
                                    }
                                    done += 1
                                    _consecutive_fails = 0  # reset on success
                                    if progress_cb:
                                        status = f"{wl} | {intensity}% | {enable_tag} | {exp_us}µs | gain {gain}"
                                        progress_cb(done, total, status, fpath, _extra)

                                except Exception as _frame_err:
                                    # --- Per-frame fault tolerance ---
                                    # Log the skip but do NOT raise — keep the sweep alive,
                                    # unless too many frames fail back-to-back.
                                    _consecutive_fails += 1
                                    skip_msg = (
                                        f"SKIP {wl} {intensity}% {exp_us}µs gain{gain}: "
                                        f"{type(_frame_err).__name__}: {_frame_err}"
                                        f"  [consecutive_fails={_consecutive_fails}/{_MAX_CONSECUTIVE_FAILS}]"
                                    )
                                    done += 1  # count as done so progress bar stays accurate
                                    if progress_cb:
                                        progress_cb(done, total, f"⚠ {skip_msg}", None, None)

                                    if _consecutive_fails >= _MAX_CONSECUTIVE_FAILS:
                                        raise RuntimeError(
                                            f"Sweep aborted: {_consecutive_fails} consecutive capture failures. "
                                            f"Last error: {type(_frame_err).__name__}: {_frame_err}"
                                        )

                                    # Attempt camera recovery: stop capture, re-init params
                                    try:
                                        if hasattr(self.camera, "stop_capture"):
                                            self.camera.stop_capture()
                                        time.sleep(2.0)
                                        self.camera.set_params(exp_us, gain)
                                    except Exception:
                                        pass
        finally:
            self.all_off()
            if progress_cb:
                final_msg = "Sweep complete." if done == total else "Sweep stopped."
                progress_cb(done, total, final_msg, None)