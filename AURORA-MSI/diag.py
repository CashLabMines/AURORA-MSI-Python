"""
AURORA-HSI

Open-source hyperspectral imaging system for quantitative optical characterization
of biological, chemical, and material samples.

Institution:
  Colorado School of Mines
  Chemical & Biological Engineering
  CASH Lab

File: diag.py
License: MIT (see LICENSE file in project root)
Copyright (c) 2026 Dr. Kevin Cash
"""
import os
import sys
import threading
import time
import traceback

try:
    import psutil
    _HAS_PSUTIL = True
except ImportError:
    _HAS_PSUTIL = False
    print("[DIAG] WARNING: psutil not installed. RAM/CPU monitoring disabled. "
          "Run: pip install psutil")

_PROC = psutil.Process(os.getpid()) if _HAS_PSUTIL else None
_T0 = time.time()


def _ram_mb() -> str:
    if not _HAS_PSUTIL:
        return "n/a"
    try:
        mi = _PROC.memory_info()
        rss = mi.rss / 1024 / 1024
        vms = mi.vms / 1024 / 1024
        sys_pct = psutil.virtual_memory().percent
        return f"RSS={rss:.1f}MB VMS={vms:.1f}MB SYS={sys_pct:.1f}%"
    except Exception as e:
        return f"err({e})"


def _cpu_pct() -> str:
    if not _HAS_PSUTIL:
        return "n/a"
    try:
        proc_pct = _PROC.cpu_percent(interval=None)
        sys_pct  = psutil.cpu_percent(interval=None)
        return f"proc={proc_pct:.1f}% sys={sys_pct:.1f}%"
    except Exception as e:
        return f"err({e})"


def _disk() -> str:
    if not _HAS_PSUTIL:
        return "n/a"
    try:
        d = psutil.disk_usage(os.path.splitdrive(sys.executable)[0] or "/")
        free_gb = d.free / 1024**3
        pct = d.percent
        return f"free={free_gb:.2f}GB used={pct:.1f}%"
    except Exception as e:
        return f"err({e})"


def _threads() -> str:
    n = threading.active_count()
    names = [t.name for t in threading.enumerate()]
    return f"{n} active: {names}"


def _uptime() -> str:
    elapsed = time.time() - _T0
    m, s = divmod(int(elapsed), 60)
    h, m = divmod(m, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


def log(tag: str = "", level: str = "INFO"):
    """Print a one-line system snapshot. Call from anywhere."""
    prefix = f"[DIAG][{level}][{tag}][up={_uptime()}]"
    print(f"{prefix} RAM: {_ram_mb()} | CPU: {_cpu_pct()} | DISK: {_disk()} | THREADS: {_threads()}")
    sys.stdout.flush()


def log_exception(tag: str = ""):
    """Call inside an except block to print full traceback + system stats."""
    tb_str = traceback.format_exc()
    print(f"[DIAG][EXCEPTION][{tag}]\n{tb_str}")
    log(tag=tag, level="ERROR")
    sys.stdout.flush()


def log_frame(tag: str, frame_shape, dtype):
    """Log info about a captured numpy frame."""
    padded = tuple(frame_shape) + (0, 0)
    h, w = (frame_shape[0], frame_shape[1]) if len(frame_shape) >= 2 else (padded[0], 0)
    size_mb = (h * w * 2) / 1024 / 1024   # uint16 = 2 bytes
    print(f"[DIAG][FRAME][{tag}] shape=({h}x{w}) dtype={dtype} size≈{size_mb:.2f}MB")
    sys.stdout.flush()


def log_tiff_write(tag: str, path: str, size_bytes: int = 0):
    """Log a TIFF write event with file size."""
    mb = size_bytes / 1024 / 1024 if size_bytes else 0
    print(f"[DIAG][TIFF][{tag}] path={path!r} size≈{mb:.2f}MB")
    log(tag=f"TIFF/{tag}")


def watch(interval_s: float = 10.0):
    """Start a background daemon thread that prints stats every interval_s seconds."""
    def _loop():
        while True:
            time.sleep(interval_s)
            log(tag="WATCHDOG")
    t = threading.Thread(target=_loop, daemon=True, name="DiagWatchdog")
    t.start()
    print(f"[DIAG] Watchdog started (every {interval_s:.0f}s)")
