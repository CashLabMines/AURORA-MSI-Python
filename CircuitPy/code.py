# code.py — Raspberry Pi Pico 2 W (CircuitPython)
# USB-serial command parser to control pins locally, now with batch support.
#
# Command syntax (newline-terminated, case-insensitive):
#   MODE   <pin|pins> <INPUT|OUTPUT|PULLUP|PULLDOWN>
#   SET    <pin|pins> <HIGH|LOW>
#   READ   <pin|pins>
#   PWM    <pin|pins> <duty_0to1> [freq_hz]
#   ANALOG <pin|pins>
#   BLINK  <pin|pins> <count> <seconds>
#   LED    <ON|OFF|BLINK> [count] [seconds]
#   BATCH  <cmd1> ; <cmd2> ; ...      (each subcommand is one of the above)
#
# Where <pin|pins> can be:
#   - a single number (e.g., 15)
#   - a comma-separated list (e.g., 1,2,5)
#   - ranges or mixed (e.g., 3-6,9,12-14)
#
# Examples:
#   MODE 15 OUTPUT
#   SET 15 HIGH
#   PWM 16 0.25 1000
#   READ 14
#   ANALOG 26
#   BLINK 25 3 0.2
#   LED BLINK 2 0.5
#   MODE 1,2,5-7 OUTPUT
#   SET 3-5,9 HIGH
#   BATCH MODE 1,2 OUTPUT; SET 1 HIGH; SET 2 LOW; LED BLINK 2 0.1
#
# Notes:
# - Pins are numeric GP numbers (e.g., 0..28). ADC pins are typically 26–28.
# - Onboard LED is accessible as 'LED' command and also as pin 25 (Digital).
# - Replies are short, single-line strings like "OK", "VALUE: ...", or "ERR: ...".
#
# Tip:
#   On your computer, open a serial terminal at the CircuitPython USB port.
#   Send lines ending with \n. Use raw mode (no local echo) if possible.

import time
import sys
import re

import board
import digitalio
import pwmio
import analogio
import supervisor

# Try to use the secondary CDC port if available (requires usb_cdc.data enabled in boot.py),
# otherwise fall back to the default console CDC (REPL) port.
try:
    import usb_cdc  # type: ignore
except Exception:
    usb_cdc = None

DATA_PORT = None
if usb_cdc and hasattr(usb_cdc, "data"):
    # Prefer the data port (separate from REPL) if it's present
    DATA_PORT = usb_cdc.data  # None if not enabled in boot.py
    # If you want to guarantee the data port exists, put this in boot.py:
    # import usb_cdc; usb_cdc.enable(console=True, data=True)

# ---------- Helpers for pin/resource management ----------

digital_pins = {}  # pin_num -> digitalio.DigitalInOut
pwm_pins = {}      # pin_num -> pwmio.PWMOut
adc_pins = {}      # pin_num -> analogio.AnalogIn

def gp_pin(pin_num: int):
    """Return the board.GP<nr> pin object or raise."""
    name = f"GP{pin_num}"
    if not hasattr(board, name):
        raise ValueError(f"No such pin GP{pin_num}")
    return getattr(board, name)

def ensure_no_conflict(pin_num: int, keep: str = ""):
    """Deinit any existing resource on this pin unless it's 'keep' ('digital', 'pwm', 'adc')."""
    if keep != "digital" and pin_num in digital_pins:
        try:
            digital_pins[pin_num].deinit()
        except Exception:
            pass
        digital_pins.pop(pin_num, None)
    if keep != "pwm" and pin_num in pwm_pins:
        try:
            pwm_pins[pin_num].deinit()
        except Exception:
            pass
        pwm_pins.pop(pin_num, None)
    if keep != "adc" and pin_num in adc_pins:
        try:
            adc_pins[pin_num].deinit()
        except Exception:
            pass
        adc_pins.pop(pin_num, None)

def get_digital(pin_num: int, direction: str = "INPUT", pull: str = "NONE"):
    """Create/reuse a DigitalInOut with requested direction/pull."""
    ensure_no_conflict(pin_num, keep="digital")
    dio = digital_pins.get(pin_num)
    if dio is None:
        dio = digitalio.DigitalInOut(gp_pin(pin_num))
        digital_pins[pin_num] = dio

    # Configure direction/pull
    direction = direction.upper()
    pull = pull.upper()
    if direction == "INPUT":
        dio.direction = digitalio.Direction.INPUT
        dio.pull = None
        if pull == "PULLUP":
            dio.pull = digitalio.Pull.UP
        elif pull == "PULLDOWN":
            dio.pull = digitalio.Pull.DOWN
    elif direction == "OUTPUT":
        dio.direction = digitalio.Direction.OUTPUT
    else:
        raise ValueError("Direction must be INPUT or OUTPUT")
    return dio

def get_pwm(pin_num: int, freq: int = 1000):
    """Create/reuse a PWMOut on the pin at given freq."""
    ensure_no_conflict(pin_num, keep="pwm")
    pwm = pwm_pins.get(pin_num)
    if pwm is None:
        pwm = pwmio.PWMOut(gp_pin(pin_num), frequency=freq, duty_cycle=0)
        pwm_pins[pin_num] = pwm
    else:
        if pwm.frequency != freq:
            pwm.frequency = freq
    return pwm

def get_adc(pin_num: int):
    """Create/reuse an AnalogIn on the pin."""
    ensure_no_conflict(pin_num, keep="adc")
    adc = adc_pins.get(pin_num)
    if adc is None:
        adc = analogio.AnalogIn(gp_pin(pin_num))
        adc_pins[pin_num] = adc
    return adc

# Onboard LED helper
def onboard_led():
    try:
        led = digital_pins.get(-1)
        if led is None:
            led_io = digitalio.DigitalInOut(board.LED)
            led_io.direction = digitalio.Direction.OUTPUT
            digital_pins[-1] = led_io
        return digital_pins[-1]
    except Exception as e:
        raise RuntimeError("Onboard LED not available") from e

# ---------- Serial I/O ----------

_rx_buffer = bytearray()

def _readline_nonblocking():
    """Return a line (bytes) if available, else None. Works with data CDC or console CDC."""
    # Prefer data CDC if available
    if DATA_PORT is not None and DATA_PORT.connected:
        if DATA_PORT.in_waiting > 0:
            chunk = DATA_PORT.read(DATA_PORT.in_waiting)
            if chunk:
                _rx_buffer.extend(chunk)
    else:
        # Console CDC (REPL) port
        if supervisor.runtime.serial_bytes_available:
            ch = sys.stdin.read(1)
            if ch:
                _rx_buffer.extend(ch.encode("utf-8"))

    # Look for newline
    nl = _rx_buffer.find(b"\n")
    if nl != -1:
        line = _rx_buffer[:nl + 1]
        # CircuitPython-safe buffer shrink (no 'del' on slices)
        _rx_buffer[:] = _rx_buffer[nl + 1:]
        return line
    return None

def _write_line(s: str):
    s = s.rstrip("\r\n") + "\r\n"
    data = s.encode("utf-8")
    if DATA_PORT is not None and DATA_PORT.connected:
        DATA_PORT.write(data)
        try:
            DATA_PORT.flush()  # ok on usb_cdc; ignored if not present
        except AttributeError:
            pass
    else:
        # sys.stdout in CircuitPython may not have flush()
        try:
            sys.stdout.write(s)
            if hasattr(sys.stdout, "flush"):
                sys.stdout.flush()
        except Exception:
            pass

# ---------- Utilities: pin set parsing ----------

def _parse_pin_set(token: str):
    """
    Parse pin token into a list of ints.
    Supports single numbers, comma lists, ranges (e.g., '3-6'), or mixed: '1,3-5,8'.
    """
    token = token.strip()
    if not token:
        raise ValueError("No pin(s) specified")
    pins = []
    parts = token.split(",")
    for part in parts:
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            a_str, b_str = part.split("-", 1)
            a = int(a_str)
            b = int(b_str)
            if b < a:
                a, b = b, a
            for p in range(a, b + 1):
                _validate_pin(p)
                pins.append(p)
        else:
            p = int(part)
            _validate_pin(p)
            pins.append(p)
    # remove duplicates but preserve order
    seen = set()
    uniq = []
    for p in pins:
        if p not in seen:
            uniq.append(p)
            seen.add(p)
    return uniq

def _validate_pin(pin: int):
    if pin < 0 or pin > 28:
        raise ValueError(f"Invalid pin GP{pin} (0..28)")

# ---------- Command handling ----------

_ws = re.compile(r"\s+")

def handle_command(line: str):
    line = line.strip()
    if not line:
        return

    #parts = _ws.split(line, maxsplit=1)
    parts = _ws.split(line, 1)  # positional maxsplit (CircuitPython-safe)
    cmd = parts[0].upper()
    rest = parts[1] if len(parts) > 1 else ""

    try:
        if cmd == "BATCH":
            # BATCH <cmd1> ; <cmd2> ; ...
            if not rest:
                raise ValueError("Usage: BATCH <cmd1> ; <cmd2> ; ...")
            subcmds = [c.strip() for c in rest.split(";") if c.strip()]
            if not subcmds:
                raise ValueError("No subcommands in BATCH")
            count = 0
            for sc in subcmds:
                try:
                    handle_command(sc)  # writes its own responses
                    count += 1
                except Exception as e:
                    _write_line(f"ERR in BATCH: {e}")
            _write_line(f"OK BATCH {count}/{len(subcmds)}")
            return

        # Re-tokenize for non-BATCH handlers
        parts = _ws.split(line)
        cmd = parts[0].upper()
        args = parts[1:]

        if cmd == "MODE":
            # MODE <pins> <INPUT|OUTPUT|PULLUP|PULLDOWN>
            if len(args) != 2:
                raise ValueError("Usage: MODE <pin|pins> <INPUT|OUTPUT|PULLUP|PULLDOWN>")
            pins = _parse_pin_set(args[0])
            mode = args[1].upper()
            for pin in pins:
                if mode in ("INPUT", "OUTPUT"):
                    get_digital(pin, direction=mode)
                elif mode in ("PULLUP", "PULLDOWN"):
                    get_digital(pin, direction="INPUT", pull=mode)
                else:
                    raise ValueError("Mode must be INPUT, OUTPUT, PULLUP, or PULLDOWN")
            _write_line("OK")

        elif cmd == "SET":
            # SET <pins> <HIGH|LOW>
            if len(args) != 2:
                raise ValueError("Usage: SET <pin|pins> <HIGH|LOW>")
            pins = _parse_pin_set(args[0])
            level = args[1].upper()
            val = (level == "HIGH")
            if level not in ("HIGH", "LOW"):
                raise ValueError("Level must be HIGH or LOW")
            for pin in pins:
                dio = get_digital(pin, direction="OUTPUT")
                dio.value = val
            _write_line("OK")

        elif cmd == "READ":
            # READ <pins>
            if len(args) != 1:
                raise ValueError("Usage: READ <pin|pins>")
            pins = _parse_pin_set(args[0])
            for pin in pins:
                dio = get_digital(pin, direction="INPUT")
                _write_line(f"VALUE GP{pin}: {1 if dio.value else 0}")

        elif cmd == "PWM":
            # PWM <pins> <duty_0to1> [freq_hz]
            if len(args) < 2:
                raise ValueError("Usage: PWM <pin|pins> <duty_0to1> [freq_hz]")
            pins = _parse_pin_set(args[0])
            duty = float(args[1])
            if not 0.0 <= duty <= 1.0:
                raise ValueError("duty must be between 0.0 and 1.0")
            freq = int(args[2]) if len(args) >= 3 else 1000
            dc = int(duty * 65535)
            for pin in pins:
                pwm = get_pwm(pin, freq=freq)
                pwm.duty_cycle = dc
            _write_line("OK")

        elif cmd == "ANALOG":
            # ANALOG <pins>
            if len(args) != 1:
                raise ValueError("Usage: ANALOG <pin|pins>")
            pins = _parse_pin_set(args[0])
            for pin in pins:
                adc = get_adc(pin)
                val = adc.value  # 0..65535
                volts = (val / 65535.0) * 3.3  # default 3.3V ref
                _write_line(f"ANALOG GP{pin}: {val} ({volts:.3f} V)")

        elif cmd == "BLINK":
            # BLINK <pins> <count> <seconds>
            if len(args) != 3:
                raise ValueError("Usage: BLINK <pin|pins> <count> <seconds>")
            pins = _parse_pin_set(args[0])
            count = int(args[1])
            period = float(args[2])
            # Blink all listed pins in lockstep
            dios = [get_digital(pin, direction="OUTPUT") for pin in pins]
            for _ in range(count):
                for d in dios:
                    d.value = True
                time.sleep(period)
                for d in dios:
                    d.value = False
                time.sleep(period)
            _write_line("OK")

        elif cmd == "LED":
            # LED <ON|OFF|BLINK> [count] [seconds]
            if len(args) < 1:
                raise ValueError("Usage: LED <ON|OFF|BLINK> [count] [seconds]")
            action = args[0].upper()
            led = onboard_led()
            if action == "ON":
                led.value = True
            elif action == "OFF":
                led.value = False
            elif action == "BLINK":
                count = int(args[1]) if len(args) >= 2 else 2
                period = float(args[2]) if len(args) >= 3 else 0.25
                for _ in range(count):
                    led.value = True
                    time.sleep(period)
                    led.value = False
                    time.sleep(period)
            else:
                raise ValueError("LED action must be ON, OFF, or BLINK")
            _write_line("OK")

        elif cmd in ("HELP", "?"):
            _write_line(
                "CMDS: MODE, SET, READ, PWM, ANALOG, BLINK, LED, BATCH | "
                "Pins support lists/ranges (e.g., 1,2,5-7). "
                "BATCH uses ';' to separate commands."
            )

        else:
            raise ValueError(f"Unknown command '{cmd}'. Try HELP.")

    except Exception as e:
        _write_line(f"ERR: {e}")

# ---------- Main loop ----------

# Friendly banner (printed once on connect)
_write_line("Pico2W GPIO daemon ready. Type HELP for commands.")

# Idle heartbeat on LED (very dim blink using time base)
_led_last = time.monotonic()
_led_state = False
try:
    hb = onboard_led()
except Exception:
    hb = None

while True:
    # Non-blocking serial read
    raw = _readline_nonblocking()
    if raw is not None:
        try:
            line = raw.decode("utf-8", "ignore")   # positional args (encoding, errors)
        except Exception:
            line = raw.decode("latin-1", "ignore")
        handle_command(line)

    # Tiny heartbeat every ~2 seconds (doesn't steal the LED if user uses LED cmd)
    now = time.monotonic()
    if hb is not None and (now - _led_last) >= 2.0:
        _led_last = now
        _led_state = not _led_state
        try:
            hb.value = _led_state
        except Exception:
            hb = None

    time.sleep(0.001)
