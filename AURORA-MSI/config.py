"""
AURORA-HSI

Open-source hyperspectral imaging system for quantitative optical characterization
of biological, chemical, and material samples.

Institution:
  Colorado School of Mines
  Chemical & Biological Engineering
  CASH Lab

File: config.py
License: MIT (see LICENSE file in project root)
Copyright (c) 2026 Dr. Kevin Cash
"""
# config.py


class Config:
    """Central configuration namespace for Aurora-HSI.

    All values are class attributes — import and reference as Config.ATTR.
    Nothing here should be instantiated.
    """

    # ---- Serial (Raspberry Pi Pico) ----
    PICO_PORT: str = "COM12"
    BAUD: int = 115200
    SERIAL_TIMEOUT: float = 1.0

    # ---- PWM for LEDs ----
    PWM_FREQ: int = 1000  # Hz

    # ---- Camera limits ----
    EXPOSURE_MIN: int = 32
    EXPOSURE_MAX: int = 2_000_000_000
    GAIN_MIN: int = 0
    GAIN_MAX: int = 450
    COOLER_MIN: int = -20
    COOLER_MAX: int = 30

    # ---- Unit conversion: label -> microseconds multiplier ----
    UNIT_CONVERSION: dict = {
        "µs": 1,
        "ms": 1_000,
        "s":  1_000_000,
    }

    # ---- Paths ----
    BASE_IMAGE_DIR: str = r"C:/Users/CashLab/Pictures/"

    # ---- LED wavelength labels -> Pico GPIO pin numbers ----
    LED_PINS: dict = {
        "367 nm":  1,
        "405 nm":  2,
        "448 nm":  3,
        "470 nm":  4,
        "505 nm":  5,
        "530 nm":  6,
        "568 nm":  7,
        "591 nm":  8,
        "617 nm":  9,
        "627 nm": 10,
        "655 nm": 11,
        "740 nm": 12,
        "850 nm": 13,
        "940 nm": 14,
    }

    # ---- Eight section enables -> Pico GPIO pin numbers ----
    EN_PINS: dict = {
        "Enable1": 15,
        "Enable2": 16,
        "Enable3": 17,
        "Enable4": 18,
        "Enable5": 19,
        "Enable6": 20,
        "Enable7": 21,
        "Enable8": 22,
    }

    # ---- Filter wheel slots ----
    FILT_PINS: dict = {
        "435 nm":  0,
        "455 nm":  1,
        "495 nm":  2,
        "515 nm":  3,
        "530 nm":  4,
        "550 nm":  5,
        "570 nm":  6,
        "590 nm":  7,
        "610 nm":  8,
        "630 nm":  9,
        "645 nm": 10,
        "665 nm": 11,
        "695 nm": 12,
    }