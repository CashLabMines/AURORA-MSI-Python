"""
AURORA-HSI

Open-source hyperspectral imaging system for quantitative optical characterization
of biological, chemical, and material samples.

Institution:
  Colorado School of Mines
  Chemical & Biological Engineering
  CASH Lab

File: leds.py
License: MIT (see LICENSE file in project root)
Copyright (c) 2026 Dr. Kevin Cash
"""
# leds.py
import time
import serial
from typing import Iterable

def _pins_token(pins: Iterable[int]) -> str:
    return ",".join(str(p) for p in pins)

class PicoLEDs:
    # Maximum seconds to wait for a serial write() to complete.
    # Without this, a Pico hiccup can freeze the entire sweep indefinitely.
    WRITE_TIMEOUT: float = 5.0

    def __init__(self, port: str, baud: int = 115200, timeout: float = 1.0):
        self.port_name = port
        self._baud = baud
        self._timeout = timeout
        self.ser = serial.Serial(
            port, baudrate=baud, timeout=timeout, write_timeout=self.WRITE_TIMEOUT
        )

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()

    def _reconnect(self) -> None:
        """Close and reopen the serial port. Called automatically on write failure."""
        try:
            self.ser.close()
        except Exception:
            pass
        try:
            self.ser = serial.Serial(
                self.port_name,
                baudrate=self._baud,
                timeout=self._timeout,
                write_timeout=self.WRITE_TIMEOUT,
            )
        except Exception as e:
            raise RuntimeError(f"Could not reconnect to Pico on {self.port_name}: {e}") from e

    def _send(self, cmd: str) -> str:
        if not self.ser or not self.ser.is_open:
            self._reconnect()

        # Discard any stale responses that accumulated from previous commands.
        # Without this, unread Pico ACKs pile up in the OS receive buffer over
        # a long sweep and readline() eventually returns old, mismatched data.
        try:
            self.ser.reset_input_buffer()
        except Exception:
            self._reconnect()

        try:
            self.ser.write((cmd + "\n").encode("utf-8"))
            self.ser.flush()
        except serial.SerialTimeoutException:
            self._reconnect()
            self.ser.write((cmd + "\n").encode("utf-8"))
            self.ser.flush()
        except Exception:
            raise

        # Allow device time to respond; loop for a short window
        deadline = time.time() + 0.2
        resp = ""
        while time.time() < deadline:
            line = self.ser.readline().decode(errors="ignore").strip()
            if line:
                resp = line
                break

        # No response — retry once. A silently dropped command leaves the LED
        # in the wrong state; one retry recovers most transient Pico hiccups.
        if not resp:
            time.sleep(0.05)
            try:
                self.ser.write((cmd + "\n").encode("utf-8"))
                self.ser.flush()
            except Exception:
                pass
            deadline2 = time.time() + 0.25
            while time.time() < deadline2:
                line = self.ser.readline().decode(errors="ignore").strip()
                if line:
                    resp = line
                    break

        return resp

    def batch(self, commands: list[str]) -> None:
        if not commands:
            return
        joined = " ; ".join(commands)
        self._send(f"BATCH {joined}")
    
    # ---- Enables (use SET) ----
    def set_enables(self, en_pins_all: Iterable[int], en_pins_on: Iterable[int]) -> None:
        """Drive a subset of enable pins HIGH, all others LOW."""
        all_set = set(en_pins_all)
        on_set  = set(en_pins_on)
        off_set = list(all_set - on_set)
        on_list = list(on_set)
        cmds = []
        if off_set:
            tok = _pins_token(off_set)
            cmds += [f"MODE {tok} OUTPUT", f"SET {tok} LOW"]
        if on_list:
            tok = _pins_token(on_list)
            cmds += [f"MODE {tok} OUTPUT", f"SET {tok} HIGH"]
        self.batch(cmds)
    
    def all_enables_low(self, en_pins_all: Iterable[int]) -> None:
        tok = _pins_token(en_pins_all)
        if tok:
            self.batch([f"MODE {tok} OUTPUT", f"SET {tok} LOW"])
    
    # ---- LEDs (use PWM) ----
    def pwm(self, led_pins: Iterable[int], duty_0to1: float, freq_hz: int) -> None:
        tok = _pins_token(led_pins)
        if tok:
            # MODE not strictly required before PWM, but harmless; keeps state tidy
            self.batch([f"MODE {tok} OUTPUT", f"PWM {tok} {duty_0to1:.6f} {int(freq_hz)}"])
    
    def all_leds_off(self, led_pins_all: Iterable[int], freq_hz: int) -> None:
        """Set all LED pins to PWM duty 0."""
        self.pwm(led_pins_all, 0.0, freq_hz)
    
    def close(self):
        try:
            self.ser.close()
        except Exception:
            pass
