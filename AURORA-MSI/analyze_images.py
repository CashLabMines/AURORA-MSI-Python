"""
AURORA-HSI

Open-source hyperspectral imaging system for quantitative optical characterization
of biological, chemical, and material samples.

Institution:
  Colorado School of Mines
  Chemical & Biological Engineering
  CASH Lab

File: analyze_images.py
License: MIT (see LICENSE file in project root)
Copyright (c) 2026 Dr. Kevin Cash
"""
import argparse
import csv
import os
import re
import shutil
import sys
from pathlib import Path

import numpy as np

try:
    from PIL import Image
except ImportError:
    print("ERROR: Pillow is required. Install with: pip install Pillow")
    sys.exit(1)


# ---------------------------------------------------------------------------
# Filename parser (UPDATED)
# ---------------------------------------------------------------------------
# Supports BOTH:
#   light: ..._367_nm_100pct_30000us_gain105.tiff
#   dark:  ..._dark_0pct_100000us_gain105.tiff
#
# Notes:
# - wavelength is optional (present for light frames)
# - "_dark" is optional (present for dark frames)
_PATTERN = re.compile(
    r"(?:_(?P<wl>\d+(?:\.\d+)?)_nm)?"
    r"(?:_(?P<dark>dark))?"
    r"_(?P<int>\d+)pct"
    r"_(?P<exp>\d+)us"
    r"_gain(?P<gain>\d+)",
    re.IGNORECASE,
)


def parse_filename(fname: str) -> dict | None:
    """Extract metadata from filename supporting both light and dark images."""
    m = _PATTERN.search(fname)
    if not m:
        return None

    wl = m.group("wl")
    is_dark = m.group("dark") is not None

    # For dark frames, label wavelength as "dark" so they get grouped separately
    wavelength_nm = "dark" if is_dark else (wl if wl is not None else "UNKNOWN")

    return {
        "wavelength_nm": wavelength_nm,
        "intensity_pct": int(m.group("int")),
        "enables": "dark" if is_dark else "light",
        "exposure_us": int(m.group("exp")),
        "gain": int(m.group("gain")),
    }


# ---------------------------------------------------------------------------
# Image scoring
# ---------------------------------------------------------------------------
UINT16_MAX = 65535
SATURATION_THRESHOLD = 0.999  # pixel value above this fraction of max = saturated


def score_image(path: str, sat_fraction: float = 0.001) -> dict:
    """
    Load a 16-bit TIFF and compute contrast/dynamic range metrics.

    Returns a dict with:
        mean, median, std, p1, p99        — basic statistics
        dynamic_range                      — p99 - p1  (primary score)
        sat_fraction                       — fraction of pixels that are saturated
        is_penalized                       — True if saturation exceeds allowed limit
        is_underexposed                    — True if p99 < 5% of max (dark/dead image)
        score                              — final score (0 if penalized/underexposed, else dynamic_range)
        min_val, max_val                   — raw pixel min/max
        snr                                — mean / std (signal-to-noise ratio)
        cv                                 — std / mean (coefficient of variation; lower = less noise)
        sharpness                          — Laplacian variance (focus quality; higher = sharper)
        uniformity_cv                      — CV of 3x3 tile means (illumination uniformity; lower = more uniform)
    """
    try:
        img = Image.open(path)
        arr = np.array(img).astype(np.float32)
        # Normalize to 0-65535 scale regardless of bit depth (PNG=8bit, TIFF=16bit)
        if arr.max() <= 255:
            arr = arr * (65535.0 / 255.0)
        if arr.ndim == 3:
            arr = arr.mean(axis=2)  # collapse RGB/RGBA to grayscale
    except Exception as e:
        return {"error": str(e), "score": -1, "dynamic_range": 0}

    flat = arr.ravel()
    total = flat.size

    sat_threshold_val = SATURATION_THRESHOLD * UINT16_MAX
    n_sat = int(np.sum(flat >= sat_threshold_val))
    frac_sat = n_sat / total

    p1 = float(np.percentile(flat, 1))
    p99 = float(np.percentile(flat, 99))
    dynamic_range = p99 - p1

    mean_val = float(np.mean(flat))
    std_val = float(np.std(flat))

    is_penalized = frac_sat > sat_fraction
    is_underexposed = p99 < 0.05 * UINT16_MAX  # signal never gets above 5% of full range

    # SNR and CV
    snr = mean_val / max(std_val, 1e-6)
    cv = std_val / max(mean_val, 1e-6)

    # Sharpness: variance of Laplacian (higher = sharper / better focused)
    sharpness = 0.0
    if arr.ndim == 2 and arr.shape[0] > 2 and arr.shape[1] > 2:
        try:
            lap = (
                np.roll(arr, 1, 0) + np.roll(arr, -1, 0) +
                np.roll(arr, 1, 1) + np.roll(arr, -1, 1) -
                4.0 * arr
            )
            sharpness = float(np.var(lap))
        except Exception:
            sharpness = 0.0

    # Spatial uniformity: CV of 3x3 grid of tile means (lower = more uniform illumination)
    uniformity_cv = 0.0
    if arr.ndim == 2:
        try:
            h, w = arr.shape
            tile_means = []
            for ti in range(3):
                for tj in range(3):
                    r0, r1 = ti * h // 3, (ti + 1) * h // 3
                    c0, c1 = tj * w // 3, (tj + 1) * w // 3
                    tile = arr[r0:r1, c0:c1]
                    if tile.size > 0:
                        tile_means.append(float(np.mean(tile)))
            if tile_means and np.mean(tile_means) > 0:
                uniformity_cv = float(np.std(tile_means) / np.mean(tile_means))
        except Exception:
            uniformity_cv = 0.0

    disqualified = is_penalized or is_underexposed

    return {
        "mean": mean_val,
        "median": float(np.median(flat)),
        "std": std_val,
        "min_val": float(flat.min()),
        "max_val": float(flat.max()),
        "p1": p1,
        "p99": p99,
        "dynamic_range": dynamic_range,
        "sat_fraction": frac_sat,
        "is_penalized": is_penalized,
        "is_underexposed": is_underexposed,
        "snr": snr,
        "cv": cv,
        "sharpness": sharpness,
        "uniformity_cv": uniformity_cv,
        "score": 0.0 if disqualified else dynamic_range,
    }


# ---------------------------------------------------------------------------
# Main analysis
# ---------------------------------------------------------------------------
def analyze(input_folder: str, out_folder: str, sat_fraction: float):
    input_path = Path(input_folder)

    # (Already correct) analyze ALL TIFFs/PNGs in folder
    tiffs = sorted(input_path.glob("*.tiff")) + sorted(input_path.glob("*.tif")) + sorted(input_path.glob("*.png"))

    if not tiffs:
        raise RuntimeError(f"No TIFF files found in: {input_folder}")

    print(f"Found {len(tiffs)} TIFF files. Scoring...")

    # Output directories
    out_path = Path(out_folder)
    best_dir = out_path / "best_per_wavelength"
    out_path.mkdir(parents=True, exist_ok=True)
    best_dir.mkdir(exist_ok=True)

    all_rows = []
    skipped = 0

    for i, tiff in enumerate(tiffs, 1):
        fname = tiff.name
        meta = parse_filename(fname)
        if meta is None:
            print(f"  [{i}/{len(tiffs)}] SKIP (unrecognised filename): {fname}")
            skipped += 1
            continue

        metrics = score_image(str(tiff), sat_fraction)
        if "error" in metrics:
            print(f"  [{i}/{len(tiffs)}] ERROR loading {fname}: {metrics['error']}")
            skipped += 1
            continue

        row = {
            "filename": fname,
            "wavelength_nm": meta["wavelength_nm"],
            "intensity_pct": meta["intensity_pct"],
            "exposure_us": meta["exposure_us"],
            "gain": meta["gain"],
            "enables": meta["enables"],
            **{k: round(v, 4) if isinstance(v, float) else v for k, v in metrics.items()},
        }
        all_rows.append(row)

        if metrics["is_penalized"]:
            status = "PENALIZED (saturated)"
        elif metrics.get("is_underexposed"):
            status = "PENALIZED (underexposed)"
        else:
            status = f"score={metrics['score']:.0f}  snr={metrics.get('snr', 0):.1f}"
        print(f"  [{i}/{len(tiffs)}] {meta['wavelength_nm']} | {status}")

    if not all_rows:
        raise RuntimeError("No files could be scored. Check your filename format.")

    # ---- Write full summary CSV ----
    full_csv = out_path / "analysis_summary.csv"
    fieldnames = list(all_rows[0].keys())
    with open(full_csv, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(all_rows)
    print(f"\nFull summary written: {full_csv}")

    # ---- Find best per wavelength ----
    # Group by wavelength_nm (includes "dark" group)
    by_wl: dict[str, list] = {}
    for row in all_rows:
        by_wl.setdefault(row["wavelength_nm"], []).append(row)

    # Sort each group by score descending
    for wl in by_wl:
        by_wl[wl].sort(key=lambda r: r["score"], reverse=True)

    best_rows = []
    print(f"\n{'─'*70}")
    print(f"{'WAVELENGTH':>12}  {'BEST FILE':<45}  {'SCORE':>8}  {'SAT%':>6}")
    print(f"{'─'*70}")

    def wl_sort_key(x: str):
        if x.lower() == "dark":
            return (1, 0.0)  # put dark at end
        try:
            return (0, float(x))
        except Exception:
            return (0, 0.0)

    for wl in sorted(by_wl.keys(), key=wl_sort_key):
        ranked = by_wl[wl]
        best = ranked[0]
        runner = ranked[1] if len(ranked) > 1 else None

        best_file_path = input_path / best["filename"]

        # Copy best image
        dest = best_dir / best["filename"]
        shutil.copy2(str(best_file_path), str(dest))

        # Save histogram for best
        row = {
            "wavelength_nm": wl,
            "best_filename": best["filename"],
            "best_score": best["score"],
            "best_dynamic_range": best["dynamic_range"],
            "best_exposure_us": best["exposure_us"],
            "best_gain": best["gain"],
            "best_intensity_pct": best["intensity_pct"],
            "best_sat_fraction": best["sat_fraction"],
            "best_mean": best["mean"],
            "runner_up_filename": runner["filename"] if runner else "",
            "runner_up_score": runner["score"] if runner else "",
            "total_candidates": len(ranked),
        }
        best_rows.append(row)

        sat_str = f"{best['sat_fraction']*100:.3f}%"
        wl_label = f"{wl} nm" if wl not in ("dark", "UNKNOWN") else wl
        print(f"{wl_label:>12}  {best['filename']:<45}  {best['score']:>8.0f}  {sat_str:>6}")

    print(f"{'─'*70}")

    # ---- Write best summary CSV ----
    best_csv = out_path / "best_summary.csv"
    if best_rows:
        with open(best_csv, "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=list(best_rows[0].keys()))
            writer.writeheader()
            writer.writerows(best_rows)

    print(f"\nBest images copied to: {best_dir}")
    print(f"Best summary CSV:       {best_csv}")
    if skipped:
        print(f"Skipped {skipped} files (unrecognised filename or load error)")
    print(f"\nDone. {len(by_wl)} groups processed, {len(all_rows)} images scored.")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(
        description="Find the best-contrast image per wavelength from Aurora-HSI captures."
    )
    ap.add_argument("input_folder", help="Folder containing all .tiff files")
    ap.add_argument(
        "--out", default=None,
        help="Output folder (default: <input_folder>/best)"
    )
    ap.add_argument(
        "--sat", type=float, default=0.001,
        help="Max allowed saturated pixel fraction before penalizing (default: 0.001 = 0.1%%)"
    )
    args = ap.parse_args()

    out = args.out or os.path.join(args.input_folder, "best")
    try:
        analyze(args.input_folder, out, args.sat)
    except RuntimeError as e:
        print(f"ERROR: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()