"""
AURORA-HSI

Open-source hyperspectral imaging system for quantitative optical characterization
of biological, chemical, and material samples.

Institution:
  Colorado School of Mines
  Chemical & Biological Engineering
  CASH Lab

File: settings_store.py
License: MIT (see LICENSE file in project root)
Copyright (c) 2026 Dr. Kevin Cash
"""
# settings_store.py — simple JSON settings (no external deps)
import os, json

APP_DIR = os.path.join(os.path.expanduser("~"), ".openivis")
SETTINGS_PATH = os.path.join(APP_DIR, "settings.json")

DEFAULTS = {
    "intensity_pct": 100.0,
    "exposure_value": 1000.0,   # numeric value only
    "exposure_unit": "µs",      # "µs", "ms", "s"
    "gain": 100,
}

def load_settings() -> dict:
    try:
        if os.path.exists(SETTINGS_PATH):
            with open(SETTINGS_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
                return {**DEFAULTS, **data}
    except Exception:
        pass
    return DEFAULTS.copy()

def save_settings(data: dict) -> None:
    try:
        os.makedirs(APP_DIR, exist_ok=True)
        with open(SETTINGS_PATH, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
    except Exception:
        # intentionally silent; persistence shouldn't crash the app
        pass
