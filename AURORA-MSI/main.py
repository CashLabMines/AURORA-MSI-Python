"""
AURORA-HSI

Open-source hyperspectral imaging system for quantitative optical characterization
of biological, chemical, and material samples.

Institution:
  Colorado School of Mines
  Chemical & Biological Engineering
  CASH Lab

File: main.py
License: MIT (see LICENSE file in project root)
Copyright (c) 2026 Dr. Kevin Cash
"""
# main.py
import argparse
import sys
import traceback

import ttkbootstrap as tb
from config import Config
from leds import PicoLEDs
from camera import Camera
from orchestrator import Orchestrator
from ui import App

APP_VERSION = "0.1.0"


def parse_args():
    ap = argparse.ArgumentParser()
    ap.add_argument("filename", nargs="?", help="ZWO ASI SDK library path")
    ap.add_argument("--port", default=Config.PICO_PORT,
                    help="Pico serial port (e.g., COM12 or /dev/ttyACM0)")
    return ap.parse_args()


def main():
    args = parse_args()

    # ---- Pico init ----
    try:
        pico = PicoLEDs(args.port, Config.BAUD, Config.SERIAL_TIMEOUT)
    except Exception:
        traceback.print_exc()
        sys.exit(1)

    # ---- Camera init ----
    try:
        cam = Camera(args.filename)
    except Exception:
        traceback.print_exc()
        try:
            pico.close()
        except Exception:
            pass
        sys.exit(1)

    # ---- Orchestrator + UI ----
    oz = Orchestrator(pico, cam, Config.LED_PINS, Config.EN_PINS)

    root = tb.Window(themename="darkly")
    root.title("Aurora-HSI Control Panel")

    app = App(root, oz, app_version=APP_VERSION)
    root.protocol("WM_DELETE_WINDOW", app.on_close)

    try:
        root.mainloop()
    except Exception:
        traceback.print_exc()
    finally:
        try:
            pico.close()
        except Exception:
            pass
        try:
            cam.close()
        except Exception:
            pass


if __name__ == "__main__":
    main()