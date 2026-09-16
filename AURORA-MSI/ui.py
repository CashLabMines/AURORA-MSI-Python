"""
AURORA-HSI

Open-source hyperspectral imaging system for quantitative optical characterization
of biological, chemical, and material samples.

Institution:
  Colorado School of Mines
  Chemical & Biological Engineering
  CASH Lab

File: ui.py
License: MIT (see LICENSE file in project root)
Copyright (c) 2026 Dr. Kevin Cash
"""
import time
import threading
from PIL import Image, ImageTk
import ttkbootstrap as tb
try:
    from analyze_images import analyze as _analyze_images
    _HAS_ANALYZE = True
except ImportError:
    _HAS_ANALYZE = False
from ttkbootstrap.constants import *
from ttkbootstrap.dialogs import Messagebox
from config import Config
from settings_store import load_settings, save_settings
from tkinter import filedialog
import os
import csv
import tkinter as tk
import psutil

# Optional (best-effort) access to ZWO ASI control constants for telemetry.
try:
    import zwoasi as asi  # type: ignore
except Exception:
    asi = None
PANEL_WIDTH = 200
PANEL_PADX = 8
PANEL_PADY = 8
THEMES = ("darkly", "sandstone")  # Dark / Light

class App:
    def __init__(self, root, orchestrator, app_version: str = "0.1.0"):
        self.root = root
        self.oz = orchestrator
        self.timelapse_running = False
        # sweep timing
        self.first_sweep_start_time = None
        self.sweep_number = 0
        self._tl_thread = None
        
        # load persisted settings
        self.settings = load_settings()

        # Default save directory: from settings, then Config, then CWD
        default_dir = self.settings.get(
            "save_dir",
            getattr(Config, "BASE_IMAGE_DIR", os.getcwd()),
        )
        self.save_dir = os.path.abspath(default_dir)
        self.save_dir_var = tk.StringVar(value=self.save_dir)
        self._sync_save_dir()
        
        # label lists
        self.led_labels = list(Config.LED_PINS.keys())
        self.en_labels = list(Config.EN_PINS.keys())
        self.filter_labels = list(Config.FILT_PINS.keys())
        
        # state
        self.checkbox_vars = []
        self.checkboxes = []
        self.en_vars = []
        self.en_boxes = []
        self.filter_vars = []
        self.filter_boxes = []
        self.led_on_btn = None
        self.led_off_btn = None
        self.en_apply_btn = None
        self.en_disable_btn = None
        self._preview_photo = None
        self.include_dark_var = tb.BooleanVar(value=False)
        self.auto_analyze_var = tb.BooleanVar(value=True)
        
        # ========== Page container ==========
        self.main_page = tb.Frame(self.root)
        self.main_page.pack(side=TOP, fill=BOTH, expand=True)


        # ========== Three-column container (inside main_page) ==========
        cols_row = tb.Frame(self.main_page)
        cols_row.pack(side=TOP, fill=BOTH, expand=True, padx=PANEL_PADX, pady=PANEL_PADY)

        # Three column frames
        col1 = tb.Frame(cols_row)
        col2 = tb.Frame(cols_row)
        col3 = tb.Frame(cols_row)

        col1.pack(side=LEFT, fill=BOTH, expand=True, padx=(0, PANEL_PADX))
        col2.pack(side=LEFT, fill=BOTH, expand=True, padx=(0, PANEL_PADX))
        col3.pack(side=LEFT, fill=BOTH, expand=True)

        # Column 1: Enables -> Intensity -> LEDs -> Filters
        self._panel_enable(col1).pack(side=TOP, fill=X, pady=(0, PANEL_PADY))
        self._panel_intensity(col1).pack(side=TOP, fill=X, pady=(0, PANEL_PADY))
        self._panel_leds(col1).pack(side=TOP, fill=X, pady=(0, PANEL_PADY))
        self._panel_filters(col1).pack(side=TOP, fill=X, pady=(0, PANEL_PADY))

        # Column 2: Experiment (includes Timelapse) -> Comments -> Last Image Preview
        self._panel_experiment_name(col2).pack(side=TOP, fill=X, pady=(0, PANEL_PADY))
        self._panel_preview(col2).pack(side=TOP, fill=BOTH, expand=True, pady=(0, PANEL_PADY))

        # Column 3: Mode switcher (Single Capture / Timelapse / Sweep)
        self._panel_mode_switcher(col3).pack(side=TOP, fill=BOTH, expand=True, pady=(0, PANEL_PADY))

        # apply persisted settings to widgets
        self._apply_settings_to_ui()

        # snapshot initial state for Reset button 
        self._snapshot_initial_state()

        # Theme toggle + Reset at bottom (above status bar)
        self._panel_theme_toggle(self.root).pack(
            side=BOTTOM, fill=X, padx=PANEL_PADX, pady=(0, PANEL_PADY)
        )

        # bottom status bar + version (very bottom)
        self.app_version = app_version
        self._make_statusbar(self.root)
        self.set_status("Ready.")

        # Start camera telemetry polling (temperature + cooler power)
        self.root.after(1000, self._poll_camera_telemetry)
        # Re-apply cooler state from last session
        self._init_cooler_from_settings()

    # =========================================================================
    #  Theme toggle panel + logic
    # =========================================================================
    def _panel_theme_toggle(self, parent):
        outer = tb.Frame(parent)
        bar = tb.Frame(outer, padding=6)
        bar.pack(anchor=CENTER, fill=X)
        tb.Label(bar, text="Theme:", bootstyle="secondary").pack(side=LEFT, padx=(0, 8))
        self._theme_var = tb.StringVar(value=THEMES[0])  # default darkly; main starts in darkly
        
        rb_dark = tb.Radiobutton(
            bar, text="Dark", variable=self._theme_var, value=THEMES[0],
            bootstyle="secondary-toolbutton", command=self._apply_theme
        )
        rb_light = tb.Radiobutton(
            bar, text="Light", variable=self._theme_var, value=THEMES[1],
            bootstyle="secondary-toolbutton", command=self._apply_theme
        )
        rb_dark.pack(side=LEFT, padx=2)
        rb_light.pack(side=LEFT, padx=2)

        
        # Reset button at bottom, blue 
        tb.Button(
            bar,
            text="Reset",
            bootstyle="primary",
            command=self._reset_controls,
            width=10,
        ).pack(side=RIGHT, padx=(12, 0))

        return outer
    
    def _apply_theme(self, *_):
        target = self._theme_var.get()
        try:
            self.root.style.theme_use(target)
        except Exception:
            tb.Style(theme=target)
    
    # =========================================================================
    #  Shared “card” helper (targets column parent)
    # =========================================================================
    def _wrap_panel(self, parent, title: str):
        outer = tb.Frame(parent)
        card = tb.Frame(outer, bootstyle="secondary", padding=(6, 4, 6, 6))
        card.pack(fill=X, expand=False)

        tb.Label(card, text=title, font=("-size", 11, "-weight", "bold")).pack(anchor=CENTER, pady=(0, 4))
        content = tb.Frame(card)
        content.pack(anchor=CENTER, fill=X, expand=False)
        
        return outer, content
    
    # =========================================================================
    #  Column 1: Enables / Intensity / LEDs / Filters
    # =========================================================================
    def _panel_enable(self, parent):
        outer, content = self._wrap_panel(parent, "Section Enables")
        self.en_vars.clear()
        self.en_boxes.clear()

        for idx, name in enumerate(self.en_labels):
            var = tb.BooleanVar(value=False)
            r, c = idx // 4, idx % 4
            cb = tb.Checkbutton(
                content,
                text=name,
                variable=var,
                bootstyle="info-round-toggle",
            )
            cb.grid(row=r, column=c, padx=8, pady=8, sticky="w")
            cb.config(command=lambda i=idx: self._on_enable_checkbox_toggled(i))
            self.en_vars.append(var)
            self.en_boxes.append(cb)

        tb.Label(content, text="(Checked = rail ON)", bootstyle="secondary").grid(
            row=2, column=0, columnspan=4, sticky="w", padx=4, pady=(4, 0)
        )

        # Quick actions
        btnrow = tb.Frame(content)
        btnrow.grid(row=3, column=0, columnspan=4, pady=(8, 4), sticky="")

        # Store handles so we can recolor later
        self.en_apply_btn = tb.Button(
            btnrow,
            text="Apply Enables",
            bootstyle="secondary",  # start gray
            command=self._on_apply_enables_clicked,
            width=16,
        )
        self.en_apply_btn.pack(side=LEFT, padx=6)

        self.en_disable_btn = tb.Button(
            btnrow,
            text="Disable All",
            bootstyle="danger",  # start red
            command=self._on_disable_all_clicked,
            width=12,
        )
        self.en_disable_btn.pack(side=LEFT, padx=6)

        # Clear enable toggles
        self.en_clear_btn = tb.Button(
            btnrow,
            text="Clear",
            bootstyle="primary",
            width=10,
            command=self._on_clear_enables_clicked,
        )
        self.en_clear_btn.pack(side=LEFT, padx=6)

        # Initial state: disables highlighted
        self._set_enable_button_styles("disable")

        return outer

    

    def _panel_intensity(self, parent):
        outer, content = self._wrap_panel(parent, "LED Intensity")
        content.pack_configure(fill="x", expand=True)

        self.intensity_var = tb.DoubleVar(
            value=self.settings.get("intensity_pct", 100.0)
        )

        # 3 equal columns for easy centering
        for c in range(3):
            content.columnconfigure(c, weight=1)

        scale_len = PANEL_WIDTH

        self.intensity_scale = tb.Scale(
            content,
            from_=0,
            to=100,
            orient=HORIZONTAL,
            variable=self.intensity_var,
            length=scale_len,
            bootstyle="primary",
            command=self._on_intensity_change,
        )

        self.intensity_scale.grid(
            row=0,
            column=0,
            columnspan=3,
            padx=6,
            pady=(6, 4),
            sticky="ew",
        )

        duty = self.intensity_var.get() / 100.0
        self.intensity_label = tb.Label(
            content,
            text=f"{self.intensity_var.get():.0f} %  (duty {duty:.3f})",
            bootstyle="info",
            anchor="center",
        )
        self.intensity_label.grid(
            row=1, column=0, columnspan=3, padx=6, pady=(0, 6), sticky=""
        )

        # Quick percentage buttons
        btnrow = tb.Frame(content)
        btnrow.grid(row=2, column=0, columnspan=3, pady=(0, 6))

        for pct in (0, 25, 50, 75, 100):
            tb.Button(
                btnrow,
                text=f"{pct}%",
                width=5,
                bootstyle="secondary",
                command=lambda p=pct: self._set_intensity_pct(p),
            ).pack(side=LEFT, padx=4)

        return outer
    

    def _set_intensity_pct(self, pct: float):
        self.intensity_var.set(pct)
        self._on_intensity_change()
    
    def _panel_leds(self, parent):
        outer, content = self._wrap_panel(parent, "Select LED Wavelengths")
        self.checkbox_vars.clear()
        self.checkboxes.clear()

        for i in range(5):
            for j in range(3):
                idx = i * 3 + j
                if idx >= len(self.led_labels):
                    break
                label = self.led_labels[idx]
                var = tb.BooleanVar(value=False)
                cb = tb.Checkbutton(
                    content,
                    text=label,
                    variable=var,
                    bootstyle="secondary-round-toggle",
                )
                cb.grid(row=i, column=j, padx=6, pady=6)
                cb.config(command=lambda k=idx: self._on_led_checkbox_toggled(k))
                self.checkbox_vars.append(var)
                self.checkboxes.append(cb)

        # Row for On/Off/Clear buttons
        btnrow = tb.Frame(content)
        btnrow.grid(row=6, column=0, columnspan=3, pady=(10, 4))

        self.led_clear_btn = tb.Button(
            btnrow,
            text="Clear",
            bootstyle="primary",
            width=10,
            command=self._on_clear_leds_clicked,
        )
        self.led_clear_btn.pack(side=RIGHT, padx=8)

        self.led_on_btn = tb.Button(
            btnrow,
            text="On",
            bootstyle="secondary",
            width=12,
            command=self._on_power_clicked,
        )
        self.led_on_btn.pack(side=LEFT, padx=8)

        self.led_off_btn = tb.Button(
            btnrow,
            text="Off",
            bootstyle="danger",
            width=12,
            command=self._off_power_clicked,
        )
        self.led_off_btn.pack(side=LEFT, padx=8)

        self._set_onoff_button_styles("off")

        # DARK toggle — placed in the LED grid at the empty slot after the last LED
        self.dark_cb = tb.Checkbutton(
            content,
            text="DARK",
            variable=self.include_dark_var,
            bootstyle="warning-round-toggle",
        )
        self.dark_cb.grid(row=4, column=2, padx=6, pady=6)

        return outer

    def _panel_filters(self, parent):
        outer, content = self._wrap_panel(parent, "Select Filters")

        content.pack_configure(fill=BOTH, expand=True)

        for col in range(3):
            content.columnconfigure(col, weight=1)

        self.filter_vars.clear()
        self.filter_boxes.clear()

        # Filters laid out in 3 columns
        for idx, label in enumerate(self.filter_labels):
            r, c = divmod(idx, 3)
            var = tb.BooleanVar(value=False)
            cb = tb.Checkbutton(
                content,
                text=label,
                variable=var,
                bootstyle="success-round-toggle",
                command=lambda i=idx: self._on_filter_clicked(i),
            )
            cb.grid(row=r, column=c, padx=6, pady=6, sticky="w")
            self.filter_vars.append(var)
            self.filter_boxes.append(cb)

        # Find row/col of last filter (e.g. 695)
        if self.filter_labels:
            last_idx = len(self.filter_labels) - 1
            last_row, last_col = divmod(last_idx, 3)
        else:
            last_row, last_col = 0, 0

        none_col = (last_col + 1) if (last_col + 1) < 3 else 0

        self.filter_none_btn = tb.Button(
            content,
            text="None",
            bootstyle="secondary",
            width=10,
            command=self._filters_clear,
        )
        self.filter_none_btn.grid(
            row=last_row,
            column=none_col,
            padx=6,
            pady=(6, 4),
            sticky="ew",
        )
        self._update_filter_none_state()
        return outer
    
    def _panel_comments(self, parent):
        outer, content = self._wrap_panel(parent, "Comments")

        self.comment_text = tk.Text(
            content,
            height=3,
            wrap="word",
        )
        self.comment_text.pack(fill=X, padx=6, pady=6)

        return outer

    # =========================================================================
    #  Column 2 & 3 panels: Experiment, Camera, Preview, Timelapse, Full Stack
    # =========================================================================
    def _panel_experiment_name(self, parent):
        outer, content = self._wrap_panel(parent, "Experiment")

        content.columnconfigure(1, weight=1)

        # Row 0: experiment name
        tb.Label(content, text="Experiment Name:").grid(
            row=0, column=0, padx=6, pady=6, sticky="e"
        )
        self.exp_entry = tb.Entry(content, width=28, bootstyle="dark")
        self.exp_entry.grid(row=0, column=1, padx=6, pady=6, sticky="ew")
        self.exp_entry.bind("<FocusOut>", lambda e: self._save_settings())
        self.exp_entry.bind("<Return>", lambda e: self._save_settings())

        # Row 1: save directory
        tb.Label(content, text="Save Folder:").grid(
            row=1, column=0, padx=6, pady=6, sticky="w"
        )

        path_row = tb.Frame(content)
        path_row.grid(row=1, column=1, padx=6, pady=6, sticky="ew")
        path_row.columnconfigure(0, weight=1)

        self.save_dir_entry = tb.Entry(
            path_row,
            textvariable=self.save_dir_var,
            width=40,
            state="readonly",
        )
        self.save_dir_entry.grid(row=0, column=0, sticky="ew")

        tb.Button(
            path_row,
            text="Browse…",
            bootstyle="secondary",
            width=10,
            command=self._choose_save_dir,
        ).grid(row=0, column=1, padx=(6, 0), sticky="e")

        # ---- Comments (merged via separator) ----
        tb.Separator(content, orient="horizontal").grid(
            row=2, column=0, columnspan=2, sticky="ew", padx=6, pady=(6, 2))
        tb.Label(content, text="Comments",
                 font=("-size", 10, "-weight", "bold")).grid(
            row=3, column=0, columnspan=2, pady=(2, 4))
        self.comment_text = tk.Text(content, height=3, wrap="word")
        self.comment_text.grid(row=4, column=0, columnspan=2, padx=6, pady=(0, 6), sticky="ew")

        return outer
    
        
    def _panel_preview(self, parent):
        outer, content = self._wrap_panel(parent, "Last Image Preview")

        # --- Preview area — fixed-size canvas so size never changes ---
        PREVIEW_W, PREVIEW_H = 400, 300
        self._preview_canvas_w = PREVIEW_W
        self._preview_canvas_h = PREVIEW_H

        self.preview_frame = tb.Frame(content)
        self.preview_frame.pack(padx=6, pady=4)

        self.preview_label = tk.Canvas(
            self.preview_frame,
            width=PREVIEW_W,
            height=PREVIEW_H,
            bg="#1a1a2e",
            highlightthickness=0,
            cursor="hand2",
        )
        self.preview_label.pack()
        self._preview_canvas_text = self.preview_label.create_text(
            PREVIEW_W // 2, PREVIEW_H // 2,
            text="No image captured yet.",
            fill="#888888",
            font=("", 11),
        )
        self._preview_canvas_img_id = None

        self.preview_label.bind("<Button-1>", self._on_preview_clicked)

        self.preview_filename_lbl = tb.Label(
            content, text="", bootstyle="secondary",
            anchor="center", font=("", 8),
        )
        self.preview_filename_lbl.pack(fill="x", padx=6, pady=(2, 0))

        # Separator can stay PACKed because it's still in 'content'
        tb.Separator(content).pack(fill="x", padx=6, pady=(6, 8))

        # --- Cooler controls (GRID inside a dedicated subframe) ---
        grid_area = tb.Frame(content)
        grid_area.pack(fill="x", padx=6, pady=4)

        # Persisted defaults (but we do NOT force-enable cooling unless user turns it on).
        self.cooler_var = tb.BooleanVar(value=False)  # always start with cooler off
        self.target_temp_var = tb.IntVar(value=int(self.settings.get("target_temp_c", -10)))

        tb.Checkbutton(
            grid_area,
            text="Cooler On",
            variable=self.cooler_var,
            bootstyle="info-round-toggle",
            command=self._on_cooler_toggle,
        ).grid(row=0, column=0, padx=6, pady=4, sticky="w")

        tb.Label(grid_area, text="Target (°C):").grid(row=0, column=1, padx=6, pady=4, sticky="e")

        self.target_temp_spin = tb.Spinbox(
            grid_area,
            from_=Config.COOLER_MIN,
            to=Config.COOLER_MAX,
            increment=1,
            textvariable=self.target_temp_var,
            width=6,
            bootstyle="secondary",
            command=self._on_target_temp_commit,
        )
        self.target_temp_spin.grid(row=0, column=2, padx=6, pady=4, sticky="w")

        # Commit when user types a value
        self.target_temp_spin.bind("<FocusOut>", self._on_target_temp_commit)
        self.target_temp_spin.bind("<Return>", self._on_target_temp_commit)

        # Telemetry (best-effort)
        tb.Label(grid_area, text="Sensor:").grid(row=1, column=0, padx=6, pady=4, sticky="e")
        self.temp_now_lbl = tb.Label(grid_area, text="— °C", bootstyle="secondary")
        self.temp_now_lbl.grid(row=1, column=1, padx=6, pady=4, sticky="w")

        tb.Label(grid_area, text="Cooler:").grid(row=1, column=2, padx=6, pady=4, sticky="e")
        self.cooler_pwr_lbl = tb.Label(grid_area, text="— %", bootstyle="secondary")
        self.cooler_pwr_lbl.grid(row=1, column=3, padx=6, pady=4, sticky="w")

        # Make the GRID flexible (apply to grid_area, not content)
        try:
            grid_area.grid_columnconfigure(1, weight=1)
        except Exception:
            pass

        return outer


    def _update_preview(self, image_path: str):
        """Load, normalise (16-bit→8-bit), and show thumbnail in the preview card."""
        img = None
        try:
            import numpy as np
            img = Image.open(image_path)
            # Normalise 16-bit / raw integer modes to 8-bit for display
            if getattr(img, "mode", "") in ("I;16", "I;16B", "I;16L", "I"):
                arr = np.array(img, dtype=np.uint16)
                lo, hi = int(arr.min()), int(arr.max())
                if hi > lo:
                    arr8 = ((arr - lo) * (255.0 / (hi - lo))).astype(np.uint8)
                else:
                    arr8 = np.zeros_like(arr, dtype=np.uint8)
                img_conv = Image.fromarray(arr8, mode="L")
                del arr, arr8  # free large arrays immediately
                img.close()
                img = img_conv
            # Scale to fit the fixed canvas size
            cw = self._preview_canvas_w
            ch = self._preview_canvas_h
            w, h = img.size
            scale = min(cw / w, ch / h)
            new_size = (max(1, int(w * scale)), max(1, int(h * scale)))
            img_resized = img.resize(new_size, Image.LANCZOS)
            self._preview_photo = ImageTk.PhotoImage(img_resized)
            img_resized.close()
            # Draw centred on the canvas; replace any previous image
            self.preview_label.delete("all")
            self._preview_canvas_img_id = self.preview_label.create_image(
                cw // 2, ch // 2, anchor="center", image=self._preview_photo
            )
            self._last_image_path = image_path
            self.preview_filename_lbl.config(text=os.path.basename(image_path))
        except Exception as e:
            self.preview_label.delete("all")
            self.preview_label.create_text(
                self._preview_canvas_w // 2, self._preview_canvas_h // 2,
                text=f"Preview failed: {e}", fill="#888888", font=("", 10),
            )
        finally:
            if img is not None:
                try:
                    img.close()
                except Exception:
                    pass

    def _elapsed_since_first_sweep(self):
        if self.first_sweep_start_time is None:
            return "00_00_00_000"

        elapsed = time.time() - self.first_sweep_start_time

        h = int(elapsed // 3600)
        m = int((elapsed % 3600) // 60)
        s = int(elapsed % 60)
        ms = int((elapsed - int(elapsed)) * 1000)

        return f"{h:02d}_{m:02d}_{s:02d}_{ms:03d}"    
    

    # =========================================================================
    #  Column 3: Mode switcher — Single Capture / Timelapse / Sweep
    # =========================================================================
    def _panel_mode_switcher(self, parent):
        outer = tb.Frame(parent)

        # ---- 3 mode buttons ----
        btn_row = tb.Frame(outer)
        btn_row.pack(fill=X, pady=(0, PANEL_PADY))

        self._mode_btn_single = tb.Button(
            btn_row, text="Single Capture", bootstyle="primary",
            command=lambda: self._switch_mode("single"))
        self._mode_btn_tl = tb.Button(
            btn_row, text="Timelapse", bootstyle="secondary",
            command=lambda: self._switch_mode("timelapse"))
        self._mode_btn_sweep = tb.Button(
            btn_row, text="Sweep", bootstyle="secondary",
            command=lambda: self._switch_mode("sweep"))

        btn_row.columnconfigure(0, weight=1)
        btn_row.columnconfigure(1, weight=1)
        btn_row.columnconfigure(2, weight=1)
        self._mode_btn_single.grid(row=0, column=0, padx=(0, 4), sticky="ew")
        self._mode_btn_tl.grid(row=0, column=1, padx=4, sticky="ew")
        self._mode_btn_sweep.grid(row=0, column=2, padx=(4, 0), sticky="ew")

        # ---- Content frames (all fill full width) ----
        self._mode_single_frame = tb.Frame(outer)
        self._mode_tl_frame = tb.Frame(outer)
        self._mode_sweep_frame = tb.Frame(outer)

        # Build content into each frame
        self._build_single_capture_pane(self._mode_single_frame)
        self._build_timelapse_pane(self._mode_tl_frame)
        self._panel_auto_loop(self._mode_sweep_frame).pack(fill=BOTH, expand=True)

        # ---- Per-wavelength full page (sibling of mode frames, hidden by default) ----
        self._perwl_page = tb.Frame(outer)

        perwl_top = tb.Frame(self._perwl_page)
        perwl_top.pack(fill=X, padx=6, pady=(6, 4))
        tb.Button(perwl_top, text="← Back", bootstyle="secondary",
                  command=self._close_perwl_page).pack(side=LEFT)
        tb.Label(perwl_top, text="Per-Wavelength Settings",
                 font=("-size", 11, "-weight", "bold")).pack(side=LEFT, padx=12)

        tb.Separator(self._perwl_page).pack(fill=X, padx=6, pady=(0, 4))

        perwl_scroll_frame = tb.Frame(self._perwl_page)
        perwl_scroll_frame.pack(fill=BOTH, expand=True, padx=6)

        hdr = tb.Frame(perwl_scroll_frame)
        hdr.pack(fill="x", pady=(0, 2))
        tb.Label(hdr, text="Wavelength", width=12, bootstyle="secondary").pack(side=LEFT, padx=4)
        tb.Label(hdr, text="Exposures (ms, comma-sep)", width=26, bootstyle="secondary").pack(side=LEFT, padx=4)
        tb.Label(hdr, text="Intensities (%, comma-sep)", width=26, bootstyle="secondary").pack(side=LEFT, padx=4)
        tb.Label(hdr, text="Gains (comma-sep)", width=20, bootstyle="secondary").pack(side=LEFT, padx=4)

        self.sweep_perwl_exp_entries = {}
        self.sweep_perwl_int_entries = {}
        self.sweep_perwl_gain_entries = {}
        for lbl in self.led_labels:
            row_f = tb.Frame(perwl_scroll_frame)
            row_f.pack(fill="x", pady=1)
            tb.Label(row_f, text=lbl, width=12).pack(side=LEFT, padx=4)
            val_entry = tb.Entry(row_f, width=26, bootstyle="dark")
            val_entry.insert(0, "30")
            val_entry.pack(side=LEFT, padx=4)
            # unit is always ms; store None as placeholder
            self.sweep_perwl_exp_entries[lbl] = (val_entry, None)
            int_entry = tb.Entry(row_f, width=26, bootstyle="dark")
            int_entry.insert(0, "100")
            int_entry.pack(side=LEFT, padx=4)
            self.sweep_perwl_int_entries[lbl] = int_entry
            gain_entry = tb.Entry(row_f, width=20, bootstyle="dark")
            gain_entry.insert(0, "100")
            gain_entry.pack(side=LEFT, padx=4)
            self.sweep_perwl_gain_entries[lbl] = gain_entry



        # Start with single capture shown
        self._active_mode = None
        self._switch_mode("single")

        return outer

    def _switch_mode(self, mode: str):
        self._active_mode = mode
        for frame in (self._mode_single_frame, self._mode_tl_frame, self._mode_sweep_frame):
            frame.pack_forget()
        if mode == "single":
            self._mode_single_frame.pack(anchor="n", fill=X)
            self._mode_btn_single.config(bootstyle="primary")
            self._mode_btn_tl.config(bootstyle="secondary")
            self._mode_btn_sweep.config(bootstyle="secondary")
        elif mode == "timelapse":
            self._mode_tl_frame.pack(anchor="n", fill=X)
            self._mode_btn_single.config(bootstyle="secondary")
            self._mode_btn_tl.config(bootstyle="primary")
            self._mode_btn_sweep.config(bootstyle="secondary")
        elif mode == "sweep":
            self._mode_sweep_frame.pack(anchor="n", fill=X)
            self._mode_btn_single.config(bootstyle="secondary")
            self._mode_btn_tl.config(bootstyle="secondary")
            self._mode_btn_sweep.config(bootstyle="primary")

    def _build_single_capture_pane(self, parent):
        outer, content = self._wrap_panel(parent, "Single Capture")
        outer.pack(fill=X, expand=True)

        # Camera settings
        content.columnconfigure(1, weight=1)
        tb.Label(content, text="Exposure Time:").grid(row=0, column=0, padx=6, pady=6, sticky="e")
        exp_row = tb.Frame(content)
        exp_row.grid(row=0, column=1, columnspan=2, padx=6, pady=6, sticky="ew")
        self.exposure_entry = tb.Entry(exp_row, width=16, bootstyle="dark")
        self.exposure_entry.insert(0, str(self.settings.get("exposure_value", 1000.0)))
        self.exposure_entry.pack(side=LEFT)
        self.exposure_entry.bind("<FocusOut>", self._on_exposure_changed)
        self.exposure_entry.bind("<Return>", self._on_exposure_changed)
        self.exposure_unit_var = tb.StringVar(value=self.settings.get("exposure_unit", "\u00b5s"))
        tb.Combobox(exp_row, textvariable=self.exposure_unit_var,
                    values=["\u00b5s", "ms", "s"], width=5, bootstyle="secondary"
                    ).pack(side=LEFT, padx=(4, 0))
        self.exposure_unit_var.trace_add("write", lambda *_: self._save_settings())

        tb.Label(content, text="Gain:").grid(row=1, column=0, padx=6, pady=6, sticky="e")
        self.gain_entry = tb.Entry(content, width=16, bootstyle="dark")
        self.gain_entry.grid(row=1, column=1, padx=6, pady=6, sticky="ew")
        self.gain_entry.insert(0, "100")
        self.gain_entry.bind("<FocusOut>", self._on_gain_changed)
        self.gain_entry.bind("<Return>", self._on_gain_changed)

        # Capture button
        tb.Button(content, text="Capture Image", bootstyle="success",
                  command=self.capture_one, width=22,
                  ).grid(row=2, column=0, columnspan=3, padx=6, pady=(10, 8))

    def _build_timelapse_pane(self, parent):
        outer, content = self._wrap_panel(parent, "Timelapse")
        outer.pack(fill=X)

        content.columnconfigure(1, weight=1)

        tl_row = tb.Frame(content)
        tl_row.grid(row=0, column=0, columnspan=2, padx=6, pady=(6, 4))
        tb.Label(tl_row, text="Every").pack(side=LEFT, padx=(0, 4))
        self.tl_delay = tb.Entry(tl_row, width=5, bootstyle="dark")
        self.tl_delay.insert(0, "1")
        self.tl_delay.pack(side=LEFT)
        tb.Label(tl_row, text="min  for").pack(side=LEFT, padx=4)
        self.tl_duration = tb.Entry(tl_row, width=5, bootstyle="dark")
        self.tl_duration.insert(0, "10")
        self.tl_duration.pack(side=LEFT)
        tb.Label(tl_row, text="min").pack(side=LEFT, padx=(4, 0))

        # Exposure row
        tb.Label(content, text="Exposure:").grid(row=1, column=0, padx=6, pady=4, sticky="e")
        tl_exp_row = tb.Frame(content)
        tl_exp_row.grid(row=1, column=1, padx=6, pady=4, sticky="w")
        self.tl_exposure_entry = tb.Entry(tl_exp_row, width=10, bootstyle="dark")
        self.tl_exposure_entry.insert(0, "1000")
        self.tl_exposure_entry.pack(side=LEFT)
        self.tl_exposure_unit_var = tb.StringVar(value="µs")
        tb.Combobox(tl_exp_row, textvariable=self.tl_exposure_unit_var,
                    values=["µs", "ms", "s"], width=5, bootstyle="secondary"
                    ).pack(side=LEFT, padx=(4, 0))

        # Gain row
        tb.Label(content, text="Gain:").grid(row=2, column=0, padx=6, pady=4, sticky="e")
        self.tl_gain_entry = tb.Entry(content, width=10, bootstyle="dark")
        self.tl_gain_entry.insert(0, "100")
        self.tl_gain_entry.grid(row=2, column=1, padx=6, pady=4, sticky="w")

        self.tl_btn = tb.Button(content, text="Capture Timelapse", bootstyle="success",
                                command=self.toggle_timelapse, width=22)
        self.tl_btn.grid(row=3, column=0, columnspan=2, pady=(6, 4))

        self.tl_progress = tb.Progressbar(content, bootstyle="info-striped",
                                          mode="determinate", length=PANEL_WIDTH - 60)
        self.tl_progress.grid(row=4, column=0, columnspan=2, pady=(2, 2))

        self.tl_status = tb.Label(content, text="", bootstyle="secondary")
        self.tl_status.grid(row=5, column=0, columnspan=2, pady=(0, 6))

    def _panel_auto_loop(self, parent):
        outer, content = self._wrap_panel(parent, "Sweep")

        content.columnconfigure(0, weight=0)
        content.columnconfigure(1, weight=1)

        # ---- Gain ----
        gain_row = tb.Frame(content)
        gain_row.grid(row=0, column=0, columnspan=2, padx=6, pady=4, sticky="w")
        self.sweep_gain_sweep_var = tb.BooleanVar(value=False)
        tb.Checkbutton(gain_row, text="Sweep gains", variable=self.sweep_gain_sweep_var,
                       bootstyle="secondary-round-toggle",
                       command=self._on_sweep_gain_toggle).pack(side=LEFT, padx=(0, 10))

        # Single gain (shown when sweep off)
        self.sweep_gain_single_frame = tb.Frame(gain_row)
        self.sweep_gain_single_frame.pack(side=LEFT)
        tb.Label(self.sweep_gain_single_frame, text="Gain:").pack(side=LEFT, padx=(0, 4))
        self.auto_gain_entry = tb.Entry(self.sweep_gain_single_frame, width=6, bootstyle="dark")
        self.auto_gain_entry.insert(0, "100")
        self.auto_gain_entry.pack(side=LEFT)

        # Gain range (shown when sweep on, hidden by default)
        self.sweep_gain_range_frame = tb.Frame(gain_row)
        tb.Label(self.sweep_gain_range_frame, text="Min:").pack(side=LEFT, padx=(0, 2))
        self.sweep_gain_min = tb.Entry(self.sweep_gain_range_frame, width=5, bootstyle="dark")
        self.sweep_gain_min.insert(0, "0")
        self.sweep_gain_min.pack(side=LEFT, padx=(0, 6))
        tb.Label(self.sweep_gain_range_frame, text="Max:").pack(side=LEFT, padx=(0, 2))
        self.sweep_gain_max = tb.Entry(self.sweep_gain_range_frame, width=5, bootstyle="dark")
        self.sweep_gain_max.insert(0, "300")
        self.sweep_gain_max.pack(side=LEFT, padx=(0, 6))
        tb.Label(self.sweep_gain_range_frame, text="Step:").pack(side=LEFT, padx=(0, 2))
        self.sweep_gain_step = tb.Entry(self.sweep_gain_range_frame, width=5, bootstyle="dark")
        self.sweep_gain_step.insert(0, "50")
        self.sweep_gain_step.pack(side=LEFT)
        # Hidden initially
        self.sweep_gain_range_frame.pack_forget()

        # ---- Settle + Auto-analyze ----
        settle_analyze_row = tb.Frame(content)
        settle_analyze_row.grid(row=1, column=0, columnspan=2, padx=6, pady=4, sticky="w")
        tb.Label(settle_analyze_row, text="Settle (ms):").pack(side=LEFT, padx=(0, 4))
        self.auto_settle_entry = tb.Entry(settle_analyze_row, width=8, bootstyle="dark")
        self.auto_settle_entry.insert(0, "150")
        self.auto_settle_entry.pack(side=LEFT)
        tb.Separator(settle_analyze_row, orient="vertical").pack(side=LEFT, fill="y", padx=12)
        tb.Checkbutton(
            settle_analyze_row,
            text="Auto-Analyze after sweep",
            variable=self.auto_analyze_var,
            bootstyle="info-round-toggle",
        ).pack(side=LEFT)

        # ---- Wavelengths ----
        tb.Separator(content, orient="horizontal").grid(
            row=2, column=0, columnspan=2, sticky="ew", padx=6, pady=(6, 2))
        tb.Label(content, text="Wavelengths:", bootstyle="secondary").grid(
            row=3, column=0, columnspan=2, padx=6, sticky="w")

        self.sweep_wl_mode = "led_panel"  # "led_panel" or "all"
        wl_btn_row = tb.Frame(content)
        wl_btn_row.grid(row=4, column=0, columnspan=2, padx=6, pady=(2, 6))
        self.sweep_use_led_btn = tb.Button(
            wl_btn_row, text="Use Selected LED Panel", bootstyle="info", width=22,
            command=self._sweep_use_led_panel)
        self.sweep_use_led_btn.pack(side=LEFT, padx=(0, 6))
        self.sweep_use_all_btn = tb.Button(
            wl_btn_row, text="Use All", bootstyle="secondary-outline", width=10,
            command=self._sweep_use_all)
        self.sweep_use_all_btn.pack(side=LEFT)

        # ---- Intensities ----
        tb.Separator(content, orient="horizontal").grid(
            row=5, column=0, columnspan=2, sticky="ew", padx=6, pady=(4, 2))
        tb.Label(content, text="Intensities (%):", bootstyle="secondary").grid(
            row=6, column=0, columnspan=2, padx=6, sticky="w")

        int_frame = tb.Frame(content)
        int_frame.grid(row=7, column=0, columnspan=2, padx=6, pady=(2, 4), sticky="ew")
        self.sweep_int_vars = {}
        self.sweep_int_cbs = {}
        for i, pct in enumerate([10, 25, 50, 75, 100]):
            var = tb.BooleanVar(value=(pct == 100))
            self.sweep_int_vars[pct] = var
            cb = tb.Checkbutton(int_frame, text=f"{pct}%", variable=var,
                                bootstyle="secondary-round-toggle")
            cb.grid(row=0, column=i, padx=4, pady=2)
            self.sweep_int_cbs[pct] = cb

        # ---- Exposures ----
        tb.Separator(content, orient="horizontal").grid(
            row=8, column=0, columnspan=2, sticky="ew", padx=6, pady=(4, 2))

        # Mode buttons row: "Sweep Exposures" (blue by default) | "Per-Wavelength" (grey)
        self.sweep_exp_mode = tb.StringVar(value="sweep")
        exp_mode_row = tb.Frame(content)
        exp_mode_row.grid(row=9, column=0, columnspan=2, padx=6, pady=(2, 4), sticky="ew")
        exp_mode_row.columnconfigure(0, weight=1)
        exp_mode_row.columnconfigure(1, weight=1)
        self._sweep_exp_btn = tb.Button(
            exp_mode_row, text="Sweep Exposures", bootstyle="primary",
            command=lambda: self._set_sweep_exp_mode("sweep"))
        self._sweep_exp_btn.grid(row=0, column=0, sticky="ew", padx=(0, 4))
        self._perwl_exp_btn = tb.Button(
            exp_mode_row, text="Per-Wavelength", bootstyle="secondary",
            command=self._open_perwl_page)
        self._perwl_exp_btn.grid(row=0, column=1, sticky="ew", padx=(4, 0))

        # ---- Sweep exposures frame (shown by default) ----
        self.sweep_exp_sweep_frame = tb.Frame(content)
        self.sweep_exp_sweep_frame.grid(row=10, column=0, columnspan=2, padx=6, pady=(2, 4), sticky="ew")

        exp_preset_frame = tb.Frame(self.sweep_exp_sweep_frame)
        exp_preset_frame.pack(fill="x")
        self.sweep_exp_vars = {}
        presets_us = [500, 1000, 2000, 5000, 10000, 30000]
        preset_labels = ["0.5ms", "1ms", "2ms", "5ms", "10ms", "30ms"]
        for i, (us, lbl) in enumerate(zip(presets_us, preset_labels)):
            var = tb.BooleanVar(value=(us == 1000))
            self.sweep_exp_vars[us] = var
            tb.Checkbutton(exp_preset_frame, text=lbl, variable=var,
                           bootstyle="secondary-round-toggle"
                           ).grid(row=i // 3, column=i % 3, padx=4, pady=2, sticky="w")

        custom_exp_row = tb.Frame(self.sweep_exp_sweep_frame)
        custom_exp_row.pack(fill="x", pady=(2, 0))
        tb.Label(custom_exp_row, text="Custom (µs, comma-sep):").pack(side=LEFT, padx=(0, 4))
        self.sweep_exp_custom = tb.Entry(custom_exp_row, width=20, bootstyle="dark")
        self.sweep_exp_custom.pack(side=LEFT)



        # ---- CSV + info ----
        tb.Separator(content, orient="horizontal").grid(
            row=12, column=0, columnspan=2, sticky="ew", padx=6, pady=(4, 2))

        info_row = tb.Frame(content)
        info_row.grid(row=13, column=0, columnspan=2, sticky="ew", padx=6, pady=(4, 4))
        self.auto_total_lbl = tb.Label(info_row, text="Total images: —", bootstyle="secondary")
        self.auto_total_lbl.pack(side=LEFT)
        self.auto_eta_lbl = tb.Label(info_row, text="  |  ETA: —", bootstyle="secondary")
        self.auto_eta_lbl.pack(side=LEFT, padx=(8, 0))
        tb.Button(info_row, text="Recalculate", bootstyle="secondary-outline", width=12,
                  command=self._recalc_total_images).pack(side=RIGHT)

        self.auto_btn = tb.Button(
            content, text="Start Sweep", bootstyle="success", width=22,
            command=self.toggle_auto_run,
        )
        self.auto_btn.grid(row=14, column=0, columnspan=2, pady=(6, 4))

        self.auto_stop_requested = False

        self.auto_prog = tb.Progressbar(
            content, bootstyle="info-striped", mode="determinate", length=PANEL_WIDTH - 60)
        self.auto_prog.grid(row=15, column=0, columnspan=2, pady=(4, 2))

        self.auto_status = tb.Label(content, text="Ready.", bootstyle="secondary")
        self.auto_status.grid(row=16, column=0, columnspan=2, pady=(0, 6))

        # ---- Multi-Day Sweep ----
        tb.Separator(content, orient="horizontal").grid(
            row=17, column=0, columnspan=2, sticky="ew", padx=6, pady=(8, 4))
        tb.Label(content, text="Multi-Day Sweep",
                 font=("-size", 10, "-weight", "bold")).grid(
            row=18, column=0, columnspan=2, pady=(0, 6))

        md_row = tb.Frame(content)
        md_row.grid(row=19, column=0, columnspan=2, padx=6, pady=(0, 4))
        tb.Label(md_row, text="Repeat every").pack(side=LEFT, padx=(0, 4))
        self.md_interval_entry = tb.Entry(md_row, width=5, bootstyle="dark")
        self.md_interval_entry.insert(0, "24")
        self.md_interval_entry.pack(side=LEFT)
        tb.Label(md_row, text="hrs  for").pack(side=LEFT, padx=4)
        self.md_days_entry = tb.Entry(md_row, width=5, bootstyle="dark")
        self.md_days_entry.insert(0, "7")
        self.md_days_entry.pack(side=LEFT)
        tb.Label(md_row, text="days").pack(side=LEFT, padx=(4, 0))

        self.md_btn = tb.Button(
            content, text="Start Multi-Day Sweep", bootstyle="success", width=24,
            command=self.toggle_multiday_run,
        )
        self.md_btn.grid(row=20, column=0, columnspan=2, pady=(6, 4))

        self.md_prog = tb.Progressbar(
            content, bootstyle="warning-striped", mode="determinate", length=PANEL_WIDTH - 60)
        self.md_prog.grid(row=21, column=0, columnspan=2, pady=(2, 2))

        self.md_status = tb.Label(content, text="", bootstyle="secondary")
        self.md_status.grid(row=22, column=0, columnspan=2, pady=(0, 6))

        self.md_running = False
        self.md_stop_requested = False
        self._md_thread = None

        return outer

    def _sweep_use_led_panel(self):
        """Set sweep mode to use whatever is checked in the LED panel."""
        self.sweep_wl_mode = "led_panel"
        self.sweep_use_led_btn.config(bootstyle="info")
        self.sweep_use_all_btn.config(bootstyle="secondary-outline")
        # Restore saved checkbox states if we have them
        saved = getattr(self, "_sweep_all_saved_states", None)
        for i, var in enumerate(self.checkbox_vars):
            state = saved[i] if saved is not None else bool(var.get())
            var.set(state)
            self.checkboxes[i].config(
                bootstyle="info-round-toggle" if state else "secondary-round-toggle")
        self._sweep_all_saved_states = None

    def _sweep_use_all(self):
        """Set sweep mode to use all wavelengths, highlight all LED panel toggles blue."""
        self.sweep_wl_mode = "all"
        self.sweep_use_all_btn.config(bootstyle="info")
        self.sweep_use_led_btn.config(bootstyle="secondary-outline")
        # Save current states so we can restore them when switching back
        self._sweep_all_saved_states = [bool(v.get()) for v in self.checkbox_vars]
        # Check all vars and set blue — ttkbootstrap only shows colour on checked toggles
        for i, var in enumerate(self.checkbox_vars):
            var.set(True)
            self.checkboxes[i].config(bootstyle="info-round-toggle")

    def _on_sweep_gain_toggle(self):
        """Show/hide gain range vs single gain field."""
        if self.sweep_gain_sweep_var.get():
            self.sweep_gain_single_frame.pack_forget()
            self.sweep_gain_range_frame.pack(side=LEFT)
        else:
            self.sweep_gain_range_frame.pack_forget()
            self.sweep_gain_single_frame.pack(side=LEFT)

    def _sweep_gains(self) -> list[int]:
        """Return list of gain values for the sweep."""
        if not self.sweep_gain_sweep_var.get():
            try:
                return [int(float(self.auto_gain_entry.get()))]
            except ValueError:
                return [100]
        try:
            lo = int(self.sweep_gain_min.get())
            hi = int(self.sweep_gain_max.get())
            step = max(1, int(self.sweep_gain_step.get()))
            vals = list(range(lo, hi + 1, step))
            return vals if vals else [lo]
        except ValueError:
            return [100]

    def _on_sweep_exp_mode_changed(self):
        """Legacy — kept for any lingering references; delegates to new method."""
        mode = self.sweep_exp_mode.get()
        if mode == "per_wl":
            self._open_perwl_page()
        else:
            self._set_sweep_exp_mode("sweep")

    def _set_sweep_exp_mode(self, mode: str):
        """Switch to sweep-exposures mode and update button styles."""
        self.sweep_exp_mode.set(mode)
        self._sweep_exp_btn.config(bootstyle="primary")
        self._perwl_exp_btn.config(bootstyle="secondary")
        self.sweep_exp_sweep_frame.grid()

    def _open_perwl_page(self):
        """Hide the mode frames and show the per-wavelength full page."""
        self.sweep_exp_mode.set("per_wl")
        self._mode_single_frame.pack_forget()
        self._mode_tl_frame.pack_forget()
        self._mode_sweep_frame.pack_forget()
        self._perwl_page.pack(fill=X)

    def _close_perwl_page(self):
        """Go back; restore sweep mode view with per-wl button highlighted."""
        self._perwl_page.pack_forget()
        self._switch_mode("sweep")
        self._sweep_exp_btn.config(bootstyle="secondary")
        self._perwl_exp_btn.config(bootstyle="primary")
        self.sweep_exp_sweep_frame.grid_remove()

    def _sweep_perwl_fill_all(self):
        """Set all per-wavelength exposure entries to the fill value (ms, comma-sep ok)."""
        raw = self.sweep_perwl_fill_val.get().strip()
        try:
            float(raw.split(",")[0].strip())  # validate first token
        except ValueError:
            return
        selected = set(self._sweep_wavelengths())
        for lbl, (val_entry, _) in self.sweep_perwl_exp_entries.items():
            if not selected or lbl in selected:
                val_entry.delete(0, "end")
                val_entry.insert(0, raw)

    def _sweep_perwl_fill_all_int(self):
        """Set all per-wavelength intensity entries to the fill value."""
        raw = self.sweep_perwl_fill_int.get().strip()
        try:
            val = int(float(raw))
            assert 0 <= val <= 100
        except Exception:
            return
        selected = set(self._sweep_wavelengths())
        for lbl, int_entry in self.sweep_perwl_int_entries.items():
            if not selected or lbl in selected:
                int_entry.delete(0, "end")
                int_entry.insert(0, str(val))

    def _sweep_intensities_per_wl(self) -> dict[str, int]:
        """Back-compat shim — returns first intensity per wavelength."""
        multi = self._sweep_intensities_per_wl_multi()
        return {lbl: ints[0] for lbl, ints in multi.items() if ints}

    def _sweep_intensities_per_wl_multi(self) -> dict[str, list[int]]:
        """Return {wavelength_label: [intensity_pct, ...]}.
        Values entered comma-separated (e.g. "25, 75, 100").
        """
        result = {}
        selected = set(self._sweep_wavelengths())
        for lbl, int_entry in self.sweep_perwl_int_entries.items():
            if selected and lbl not in selected:
                continue
            ints = []
            for part in int_entry.get().split(","):
                part = part.strip()
                if not part:
                    continue
                try:
                    ints.append(max(0, min(100, int(float(part)))))
                except ValueError:
                    pass
            result[lbl] = ints if ints else [100]
        return result

    def _sweep_exposures_per_wl(self) -> dict[str, int]:
        """Back-compat shim — returns first exposure per wavelength in us."""
        multi = self._sweep_exposures_per_wl_multi()
        return {lbl: exps[0] for lbl, exps in multi.items() if exps}

    def _sweep_exposures_per_wl_multi(self) -> dict[str, list[int]]:
        """Return {wavelength_label: [exposure_us, ...]}.
        Values entered in ms, comma-separated (e.g. "30, 100, 300").
        """
        result = {}
        selected = set(self._sweep_wavelengths())
        for lbl, (val_entry, _) in self.sweep_perwl_exp_entries.items():
            if selected and lbl not in selected:
                continue
            exps = []
            for part in val_entry.get().split(","):
                part = part.strip()
                if not part:
                    continue
                try:
                    exps.append(max(32, int(float(part) * 1000)))
                except ValueError:
                    pass
            result[lbl] = exps if exps else [30_000]
        return result

    def _sweep_gains_per_wl_multi(self) -> dict[str, list[int]]:
        """Return {wavelength_label: [gain, ...]} from the per-wavelength gains column.
        Values entered comma-separated (e.g. "100, 150, 200").
        Falls back to the global gain list for any wavelength with an empty entry.
        """
        global_gains = self._sweep_gains()
        result = {}
        selected = set(self._sweep_wavelengths())
        for lbl, gain_entry in self.sweep_perwl_gain_entries.items():
            if selected and lbl not in selected:
                continue
            gains = []
            for part in gain_entry.get().split(","):
                part = part.strip()
                if not part:
                    continue
                try:
                    gains.append(max(0, min(Config.GAIN_MAX, int(float(part)))))
                except ValueError:
                    pass
            result[lbl] = gains if gains else global_gains
        return result

    def _sweep_wavelengths(self) -> list[str]:
        """Return the list of wavelengths for the current sweep mode."""
        if getattr(self, "sweep_wl_mode", "led_panel") == "all":
            return list(self.oz.led_map.keys())
        else:
            return self._selected_led_labels()

    def _sweep_intensities(self) -> list[int]:
        """Return sorted list of checked intensity percentages."""
        return sorted([pct for pct, var in self.sweep_int_vars.items() if var.get()])

    def _sweep_exposures_us(self) -> list[int]:
        """Return sorted list of exposure values in µs from preset checkboxes + custom field."""
        vals = [us for us, var in self.sweep_exp_vars.items() if var.get()]
        custom_text = self.sweep_exp_custom.get().strip()
        if custom_text:
            for part in custom_text.split(","):
                part = part.strip()
                if part:
                    try:
                        vals.append(int(float(part)))
                    except ValueError:
                        pass
        return sorted(set(vals))

    def _recalc_total_images(self):
        try:
            wls = self._sweep_wavelengths() or self._selected_led_labels()
            ints = self._sweep_intensities() or [100]
            per_wl_mode = self.sweep_exp_mode.get() == "per_wl"
            gains = self._sweep_gains()
            if per_wl_mode:
                total = self.oz.compute_total_images(
                    selected_led_labels=wls,
                    selected_en_labels=self._selected_en_labels(),
                    intensities=ints,
                    exposures_per_wl=self._sweep_exposures_per_wl(),
                    intensities_per_wl=self._sweep_intensities_per_wl(),
                    exposures_per_wl_multi=self._sweep_exposures_per_wl_multi(),
                    intensities_per_wl_multi=self._sweep_intensities_per_wl_multi(),
                    gains=gains,
                )
            else:
                exps = self._sweep_exposures_us() or [self._exposure_us()]
                total = self.oz.compute_total_images(
                    selected_led_labels=wls,
                    selected_en_labels=self._selected_en_labels(),
                    intensities=ints,
                    exposures_us=exps,
                    gains=gains,
                )
            self.auto_total_lbl.config(text=f"Total images: {total:,}")
            self.auto_eta_lbl.config(text="  |  ETA: —")
            self.auto_prog.configure(value=0, maximum=max(total, 1))
            self.auto_status.config(text="Total computed. Ready.")
        except Exception as e:
            self._messagebox_error("Error", f"Failed to calculate total: {e}")

    # =========================================================================
    #  Settings <-> UI
    # =========================================================================
    def _apply_settings_to_ui(self):
        self._on_intensity_change()  # updates label + saves
    
    def _save_settings(self):
        try:
            data = {
                "intensity_pct": float(self.intensity_var.get()),
                "exposure_value": float(self.exposure_entry.get()),
                "exposure_unit": self.exposure_unit_var.get(),
                "gain": int(float(self.gain_entry.get())),
                # Cooler settings are persisted if the widgets exist.
                "cooler_on": bool(getattr(self, "cooler_var", tb.BooleanVar(value=False)).get()),
                "target_temp_c": int(getattr(self, "target_temp_var", tb.IntVar(value=-10)).get()),
                # Burst capture
                "burst_count": int(getattr(self, "burst_count_var", tb.IntVar(value=1)).get()),
            }
            save_settings(data)
        except Exception:
            pass
    
    # Snapshot at startup for reset  
    def _snapshot_initial_state(self):
        self._initial_intensity = float(self.intensity_var.get())
        self._initial_exposure = self.exposure_entry.get()
        self._initial_exposure_unit = self.exposure_unit_var.get()
        self._initial_gain = self.gain_entry.get()
        self._initial_exp_name = self.exp_entry.get()
        self._initial_save_dir = self.save_dir

        self._initial_tl_delay = self.tl_delay.get()
        self._initial_tl_duration = self.tl_duration.get()
        self._initial_auto_gain = self.auto_gain_entry.get()
        self._initial_auto_settle = self.auto_settle_entry.get()

        self._initial_en_states = [bool(v.get()) for v in self.en_vars]
        self._initial_led_states = [bool(v.get()) for v in self.checkbox_vars]
        self._initial_filter_states = [bool(v.get()) for v in self.filter_vars]

        try:
            self._initial_comments = self.comment_text.get("1.0", "end").strip()
        except Exception:
            self._initial_comments = ""

    # Reset controls to startup values  
    def _reset_controls(self):
        """Reset UI controls to how they were when the app launched."""
        # stop long-running things
        self.timelapse_running = False
        self.auto_stop_requested = True

        # intensity
        try:
            self.intensity_var.set(self._initial_intensity)
            self._on_intensity_change()
        except Exception:
            pass

        # exposure / gain / experiment
        try:
            self.exposure_entry.delete(0, "end")
            self.exposure_entry.insert(0, self._initial_exposure)
        except Exception:
            pass
        try:
            self.exposure_unit_var.set(self._initial_exposure_unit)
        except Exception:
            pass
        try:
            self.gain_entry.delete(0, "end")
            self.gain_entry.insert(0, self._initial_gain)
        except Exception:
            pass
        try:
            self.exp_entry.delete(0, "end")
            self.exp_entry.insert(0, self._initial_exp_name)
        except Exception:
            pass

        # save dir
        try:
            self.save_dir = self._initial_save_dir
            self.save_dir_var.set(self._initial_save_dir)
        except Exception:
            pass

        # comments
        try:
            self.comment_text.delete("1.0", "end")
            if self._initial_comments:
                self.comment_text.insert("1.0", self._initial_comments)
        except Exception:
            pass

        # timelapse
        try:
            self.tl_delay.delete(0, "end")
            self.tl_delay.insert(0, self._initial_tl_delay)
            self.tl_duration.delete(0, "end")
            self.tl_duration.insert(0, self._initial_tl_duration)
            self.tl_btn.config(text="Start Timelapse", bootstyle="secondary", state="normal")
            self.tl_status.config(text="Ready.")
            self.tl_progress.configure(value=0)
        except Exception:
            pass

        # auto run
        try:
            self.auto_gain_entry.delete(0, "end")
            self.auto_gain_entry.insert(0, self._initial_auto_gain)
            self.auto_settle_entry.delete(0, "end")
            self.auto_settle_entry.insert(0, self._initial_auto_settle)
            self.auto_btn.config(
                text="Start Automatic Run",
                bootstyle="primary",
                state="normal",
            )
            self.auto_status.config(text="Ready.")
            self.auto_prog.configure(value=0)
            self.auto_total_lbl.config(text="Total images: —")
            self.auto_eta_lbl.config(text="  |  ETA: —")
        except Exception:
            pass

        # enables
        try:
            for i, var in enumerate(self.en_vars):
                state = self._initial_en_states[i]
                var.set(state)
                style = "info-round-toggle" if state else "secondary-round-toggle"
                self.en_boxes[i].config(bootstyle=style)
            self._set_enable_button_styles("disable")
        except Exception:
            pass

        # LEDs
        try:
            for i, var in enumerate(self.checkbox_vars):
                state = self._initial_led_states[i]
                var.set(state)
                style = "info-round-toggle" if state else "secondary-round-toggle"
                self.checkboxes[i].config(bootstyle=style)
            self._set_onoff_button_styles("off")
        except Exception:
            pass

        # filters
        try:
            for i, var in enumerate(self.filter_vars):
                state = self._initial_filter_states[i]
                var.set(state)
                self.filter_boxes[i].config(bootstyle="success-round-toggle")
            self._update_filter_none_state()
        except Exception:
            pass

        # preview
        try:
            self._preview_photo = None
            self._last_image_path = None
            self.preview_label.delete("all")
            self.preview_label.create_text(
                self._preview_canvas_w // 2, self._preview_canvas_h // 2,
                text="No image captured yet.", fill="#888888", font=("", 11),
            )
        except Exception:
            pass

        self.set_status("Controls reset to launch state.")

    # =========================================================================
    #  Small UI utilities
    # =========================================================================
    def _messagebox_error(self, title: str, msg: str):
        Messagebox.show_error(message=msg, title=title)
        self.set_status(f"Error: {msg}")
    
    def _selected_led_labels(self):
        return [self.checkboxes[i]['text'] for i, v in enumerate(self.checkbox_vars) if v.get()]
    
    def _selected_en_labels(self):
        return [self.en_boxes[i]['text'] for i, v in enumerate(self.en_vars) if v.get()]
    
    def _exposure_us(self):
        try:
            val = float(self.exposure_entry.get())
        except Exception:
            val = 1000.0
        mult = Config.UNIT_CONVERSION[self.exposure_unit_var.get()]
        return int(max(min(val * mult, Config.EXPOSURE_MAX), Config.EXPOSURE_MIN))
    
    def _gain(self):
        try:
            val = int(float(self.gain_entry.get()))
        except Exception:
            val = 100
        return int(max(min(val, Config.GAIN_MAX), Config.GAIN_MIN))
    
    def _intensity_pct(self):
        return float(self.intensity_var.get())
    
    def _on_intensity_change(self, _=None):
        pct = float(self.intensity_var.get())
        duty = pct / 100.0
        self.intensity_label.config(text=f"{pct:.0f} %  (duty {duty:.3f})")
        self._save_settings()
    
    def _on_exposure_changed(self, _=None):
        self._save_settings()
    
    def _on_gain_changed(self, _=None):
        self._save_settings()


    def _selected_filter_labels(self):
        return [
            self.filter_boxes[i]["text"]
            for i, v in enumerate(self.filter_vars)
            if v.get()
        ]

    def _on_filter_clicked(self, idx: int):
        clicked = self.filter_vars[idx].get()
        if clicked:
            for i, v in enumerate(self.filter_vars):
                if i != idx:
                    v.set(False)
        else:
            for v in self.filter_vars:
                v.set(False)
        self._update_filter_none_state()

    def _filters_clear(self):
        for v in self.filter_vars:
            v.set(False)
        self._update_filter_none_state()

    def _update_filter_none_state(self):
        any_selected = any(v.get() for v in self.filter_vars)
        if any_selected:
            # filters selected → None neutral
            self.filter_none_btn.config(bootstyle="secondary")
        else:
            # no filters → None = highlighted
            self.filter_none_btn.config(bootstyle="success")

    def _set_onoff_button_styles(self, state: str):
        """state ∈ {"neutral", "on", "off"}."""
        if self.led_on_btn is None or self.led_off_btn is None:
            return

        if state == "neutral":
            self.led_on_btn.config(bootstyle="secondary")
            self.led_off_btn.config(bootstyle="secondary")
        elif state == "on":
            self.led_on_btn.config(bootstyle="success")
            self.led_off_btn.config(bootstyle="secondary")
        elif state == "off":
            self.led_on_btn.config(bootstyle="secondary")
            self.led_off_btn.config(bootstyle="danger")

    def _set_enable_button_styles(self, state: str):
        """state ∈ {"neutral", "apply", "disable"}."""
        if self.en_apply_btn is None or self.en_disable_btn is None:
            return

        if state == "neutral":
            self.en_apply_btn.config(bootstyle="secondary")
            self.en_disable_btn.config(bootstyle="secondary")
        elif state == "apply":
            self.en_apply_btn.config(bootstyle="success")
            self.en_disable_btn.config(bootstyle="secondary")
        elif state == "disable":
            self.en_apply_btn.config(bootstyle="secondary")
            self.en_disable_btn.config(bootstyle="danger")

    def _set_active_capture_styles(self):
        """Apply enables and turn on LEDs exactly as if the user pressed
        Apply Enables and On — so hardware state and button colours are
        always consistent when a capture starts."""
        self._on_apply_enables_clicked()
        self.turn_on()
        # During capture: On = green, Off = grey
        self._set_onoff_button_styles("on")
        # Apply Enables = green, Disable All = grey
        self._set_enable_button_styles("apply")
        # Flash wavelength toggles green for checked LEDs
        for i, var in enumerate(self.checkbox_vars):
            if var.get():
                self.checkboxes[i].config(bootstyle="success-round-toggle")
        # Flash enable toggles green for checked enables
        for i, var in enumerate(self.en_vars):
            if var.get():
                self.en_boxes[i].config(bootstyle="success-round-toggle")
        # Flash intensity toggles green for checked intensities
        for pct, cb in getattr(self, "sweep_int_cbs", {}).items():
            if self.sweep_int_vars.get(pct, tb.BooleanVar()).get():
                cb.config(bootstyle="success-round-toggle")

    def _clear_active_capture_styles(self):
        """Revert LED and enable checkboxes back to their normal checked colour
        (info/blue) and reset button styles to their idle states."""
        for i, var in enumerate(self.checkbox_vars):
            self.checkboxes[i].config(bootstyle="info-round-toggle" if var.get() else "secondary-round-toggle")
        for i, var in enumerate(self.en_vars):
            self.en_boxes[i].config(bootstyle="info-round-toggle" if var.get() else "secondary-round-toggle")
        # Revert intensity toggles back to secondary (blue)
        for cb in getattr(self, "sweep_int_cbs", {}).values():
            cb.config(bootstyle="secondary-round-toggle")
        # After capture: Apply Enables = grey, Disable All = red (idle/off state)
        self._set_enable_button_styles("disable")
        # After capture: On = grey, Off = red (LEDs are now off)
        self._set_onoff_button_styles("off")

    def _on_power_clicked(self):
        self.turn_on()
        self._set_onoff_button_styles("on")

    def _off_power_clicked(self):
        self.turn_off()
        self._set_onoff_button_styles("off")    

    
    # =========================================================================
    #  Metadata Logger
    # =========================================================================
    def _log_capture_metadata(self, image_path: str, experiment_override: str = None, extra_meta: dict = None):
        """
        Append a row of metadata for each captured image to a CSV file
        inside the experiment folder.

        For timelapse and full-stack runs, experiment_override can be used
        to pass in a name like "<base>_timelapse" or "<base>_full_stack_run".
        """
        try:
            # experiment name (possibly overridden)  ### CHANGED
            exp = experiment_override or self.exp_entry.get().strip() or "experiment"

            base_dir = (
                getattr(self, "save_dir", None)
                or getattr(Config, "BASE_IMAGE_DIR", os.getcwd())
            )
            base_dir = base_dir.rstrip("/\\")
            folder = os.path.join(base_dir, exp)

            os.makedirs(folder, exist_ok=True)
            csv_path = os.path.join(folder, f"{exp}_metadata.csv")

            # Determine next run_number
            if os.path.exists(csv_path):
                last_run = 0
                try:
                    with open(csv_path, mode="r", newline="") as f:
                        reader = csv.DictReader(f)
                        last_row = None
                        for last_row in reader:
                            pass
                        if last_row is not None:
                            val = last_row.get("run_number", "") or "0"
                            last_run = int(val)
                except Exception:
                    last_run = 0
                run_number = last_run + 1
            else:
                run_number = 1

            ts = time.strftime("%Y-%m-%d %H:%M:%S")

            try:
                filt_labels = self._selected_filter_labels()
            except AttributeError:
                filt_labels = []
            filters_str = ";".join(filt_labels) if filt_labels else ""

            leds_str = ";".join(self._selected_led_labels())
            enables_str = ";".join(self._selected_en_labels())

            exp_value_str = self.exposure_entry.get().strip()
            exp_unit_str = self.exposure_unit_var.get()
            exposure_str = f"{exp_value_str} {exp_unit_str}"

            gain_val = self._gain()
            intensity_pct = self._intensity_pct()

            # NOTE: leds_str, exposure_str, gain_val, intensity_pct above are UI
            # defaults — overridden below by extra_meta if provided (sweep mode).

            sensor_temp_c = ""
            target_temp_c = ""
            try:
                import zwoasi as _asi
                cam = getattr(self.oz, "camera", None)
                if cam is not None:
                    getv = getattr(cam, "get_control_value_safe", None)
                    if callable(getv):
                        try:
                            raw_t = getv(_asi.ASI_TEMPERATURE)
                            if raw_t is not None:
                                v = float(raw_t[0] if isinstance(raw_t, (list, tuple)) else raw_t)
                                sensor_temp_c = f"{v/10.0:.1f}" if abs(v) > 100 else f"{v:.1f}"
                        except Exception:
                            pass
                        try:
                            raw_tgt = getv(_asi.ASI_TARGET_TEMP)
                            if raw_tgt is not None:
                                vt = float(raw_tgt[0] if isinstance(raw_tgt, (list, tuple)) else raw_tgt)
                                target_temp_c = f"{vt/10.0:.1f}" if abs(vt) > 100 else f"{vt:.1f}"
                        except Exception:
                            pass
            except Exception:
                pass
            if extra_meta:
                if "sensor_temp_c" in extra_meta:
                    sensor_temp_c = str(extra_meta["sensor_temp_c"])
                if "target_temp_c" in extra_meta:
                    target_temp_c = str(extra_meta["target_temp_c"])
                if "filters" in extra_meta:
                    filters_str = str(extra_meta["filters"])
                # Real per-image values — override UI widget readings
                if "exposure_us" in extra_meta:
                    us = float(extra_meta["exposure_us"])
                    ms = us / 1000.0
                    exposure_str = f"{ms:.3f} ms" if ms < 1000 else f"{us:.0f} µs"
                if "led_wavelength" in extra_meta:
                    leds_str = str(extra_meta["led_wavelength"])
                if "intensity_pct" in extra_meta:
                    intensity_pct = extra_meta["intensity_pct"]
                if "gain" in extra_meta:
                    gain_val = extra_meta["gain"]

            comments = ""
            try:
                if hasattr(self, "comment_text"):
                    comments = self.comment_text.get("1.0", "end").strip()
            except Exception:
                comments = ""

            sample_id = f"{exp}_run{run_number:04d}"

            row = {
                "timestamp": ts,
                "experiment_name": exp,
                "filter": filters_str,
                "led_wavelength": leds_str,
                "exposure": exposure_str,
                "gain": gain_val,
                "intensity_pct": intensity_pct,
                "enable_sections": enables_str,
                "sensor_temp_c": sensor_temp_c,
                "target_temp_c": target_temp_c,
                "image_path": image_path,
                "sample_id": sample_id,
                "run_number": run_number,
                "comments": comments,
            }

            file_exists = os.path.exists(csv_path)
            with open(csv_path, mode="a", newline="") as f:
                writer = csv.DictWriter(f, fieldnames=list(row.keys()))
                if not file_exists:
                    writer.writeheader()
                writer.writerow(row)

        except Exception:
            pass

    # =========================================================================
    #  Actions
    # =========================================================================
    def _apply_enables(self):
        self.oz.apply_enables(self._selected_en_labels())
    
    def _disable_all_enables(self):
        self.oz.all_off()
    
    def turn_on(self):
        self._apply_enables()
        sels_led = self._selected_led_labels()
        self.oz.set_led_intensity(sels_led, self._intensity_pct(), Config.PWM_FREQ)

        for i, var in enumerate(self.checkbox_vars):
            if var.get():
                self.checkboxes[i].config(bootstyle="success-round-toggle")
            else:
                self.checkboxes[i].config(bootstyle="secondary-round-toggle")


    def turn_off(self):
        """
        Turn off all LEDs/enables at the hardware level, but keep
        checkbox selections for later.
        """
        try:
            self.oz.all_off()
            self.set_status("All LEDs turned off (checkbox selections unchanged).")

            for i, var in enumerate(self.checkbox_vars):
                if var.get():
                    self.checkboxes[i].config(bootstyle="info-round-toggle")
                else:
                    self.checkboxes[i].config(bootstyle="secondary-round-toggle")
        except Exception as e:
            self._messagebox_error("Error", f"Failed to turn LEDs off: {e}")


    def _on_apply_enables_clicked(self):
        self._apply_enables()
        self._set_enable_button_styles("apply")

        # active toggles → green
        for i, var in enumerate(self.en_vars):
            if var.get():
                self.en_boxes[i].config(bootstyle="success-round-toggle")

    def _on_disable_all_clicked(self):
        self._disable_all_enables()
        self._set_enable_button_styles("disable")

        for i, var in enumerate(self.en_vars):
            if var.get():
                self.en_boxes[i].config(bootstyle="info-round-toggle")
            else:
                self.en_boxes[i].config(bootstyle="secondary-round-toggle")

    def _on_clear_enables_clicked(self):
        """
        Clear enables and turn them off in hardware:
        - uncheck all enable toggles
        - make them gray
        - send 'all off' to the rails
        """
        self._disable_all_enables()

        for i, var in enumerate(self.en_vars):
            var.set(False)
            self.en_boxes[i].config(bootstyle="secondary-round-toggle")

        self._set_enable_button_styles("disable")

        self.set_status(
            "Enable selections cleared and all enable rails turned off."
        )

    def _on_clear_leds_clicked(self):
        """
        Clear LED selection intent: uncheck all LED toggles and make them gray.
        Does NOT send any hardware commands.
        """
        for i, var in enumerate(self.checkbox_vars):
            var.set(False)
            self.checkboxes[i].config(bootstyle="secondary-round-toggle")

        self._set_onoff_button_styles("off")

        self.set_status("Wavelength selections cleared (hardware unchanged).")

    def _on_enable_checkbox_toggled(self, idx):
        var = self.en_vars[idx]
        if var.get():
            self.en_boxes[idx].config(bootstyle="info-round-toggle")
        else:
            self.en_boxes[idx].config(bootstyle="secondary-round-toggle")

    def _on_led_checkbox_toggled(self, idx: int):
        """User editing which wavelengths they intend to use."""
        var = self.checkbox_vars[idx]
        if var.get():
            self.checkboxes[idx].config(bootstyle="info-round-toggle")
        else:
            self.checkboxes[idx].config(bootstyle="secondary-round-toggle")




    def _sync_save_dir(self):
        """
        Make sure the chosen save_dir is used by:
        - Config.BASE_IMAGE_DIR
        - Orchestrator (if it has a base_dir/base_image_dir)
        - Camera (if it tracks a save/base dir)
        """
        # normalize from the StringVar
        self.save_dir = os.path.abspath(self.save_dir_var.get())

        # Update global config
        try:
            Config.BASE_IMAGE_DIR = self.save_dir
        except Exception:
            pass

        # Try to keep orchestrator in sync
        try:
            if hasattr(self.oz, "base_image_dir"):
                self.oz.base_image_dir = self.save_dir
            if hasattr(self.oz, "base_dir"):
                self.oz.base_dir = self.save_dir

            cam = getattr(self.oz, "camera", None)
            if cam is not None:
                if hasattr(cam, "base_image_dir"):
                    cam.base_image_dir = self.save_dir
                if hasattr(cam, "save_dir"):
                    cam.save_dir = self.save_dir
        except Exception:
            pass


    def _choose_save_dir(self):
        """Open a folder picker and update the base image directory."""
        initial = self.save_dir or getattr(Config, "BASE_IMAGE_DIR", os.getcwd())
        new_dir = filedialog.askdirectory(
            parent=self.root,
            initialdir=initial,
            title="Select image save folder",
        )
        if not new_dir:
            return

        new_dir = os.path.abspath(new_dir)
        self.save_dir = new_dir
        self.save_dir_var.set(new_dir)
        # NEW: keep Config / Orchestrator / Camera in sync
        self._sync_save_dir()


        try:
            Config.BASE_IMAGE_DIR = new_dir
        except Exception:
            pass

        self.set_status(f"Save folder set to: {new_dir}")

    def _on_preview_clicked(self, event=None):
        """Open the last captured TIFF in the OS default viewer."""
        path = getattr(self, "_last_image_path", None)
        if not path or not os.path.exists(path):
            Messagebox.show_info("No Image", "No image to display yet.")
            return
        try:
            os.startfile(path)
        except Exception as e:
            self._messagebox_error("Error", f"Could not open file: {e}")
    
    def toggle_cooler(self):
        try:
            on = bool(self.cooler_var.get())
            target = int(float(self.temp_entry.get()))
            target = max(min(target, Config.COOLER_MAX), Config.COOLER_MIN)
            self.oz.camera.cooler(on, target if on else None)
        except Exception as e:
            self._messagebox_error("Error", f"Cooler error: {e}")

    def capture_one(self, tl_index: int | None = None,
                    gain_override: int | None = None,
                    exposure_us_override: int | None = None):
        """Capture one image using current selections (single run)."""
        exp = self.exp_entry.get().strip()
        if not exp:
            self._messagebox_error("Error", "Experiment name is required.")
            return
        if not self._selected_led_labels():
            self._messagebox_error("Error", "Please select at least one LED wavelength before capturing.")
            return
        if not self._selected_en_labels():
            self._messagebox_error("Error", "Please enable at least one section before capturing.")
            return
        self._sync_save_dir()

        # Pre-flight: check if output file already exists before activating hardware.
        capture_name = f"{exp}_{tl_index}" if tl_index is not None else exp
        folder_name = exp
        _folder = folder_name if tl_index is not None else exp
        import os as _os
        _expected_path = _os.path.join(
            getattr(Config, "BASE_IMAGE_DIR", ""), _folder, f"{capture_name}.tiff"
        )
        if _os.path.exists(_expected_path):
            self._messagebox_error(
                "File Already Exists",
                f"A file named '{capture_name}.tiff' already exists in '{_folder}'.\nPlease use a different experiment name to avoid overwriting your data."
            )
            return

        self._set_active_capture_styles()

        # Snapshot values now (on main thread) before handing off
        exposure_us = exposure_us_override if exposure_us_override is not None else self._exposure_us()
        gain = gain_override if gain_override is not None else self._gain()
        led_labels = self._selected_led_labels()
        en_labels = self._selected_en_labels()
        intensity_pct = self._intensity_pct()
        comments = self.comment_text.get("1.0", "end").strip()
        folder_override = folder_name if tl_index is not None else None

        def run():
            try:
                path = self.oz.capture_frame(
                    experiment=capture_name,
                    folder_override=folder_override,
                    exposure_us=exposure_us,
                    gain=gain,
                    selected_led_labels=led_labels,
                    selected_en_labels=en_labels,
                    intensity_pct=intensity_pct,
                    comments=comments,
                )
                # Turn off LEDs/enables on background thread immediately after
                # capture so hardware is off as soon as the image is saved.
                try:
                    self.oz.all_off()
                except Exception:
                    pass
                def done():
                    self.set_status(f"Saved: {path}")
                    self._log_capture_metadata(path)
                    self._update_preview(path)
                    self._clear_active_capture_styles()
                self.root.after(0, done)
            except Exception as e:
                # Still turn off hardware even on failure
                try:
                    self.oz.all_off()
                except Exception:
                    pass
                err_msg = str(e)
                self.root.after(0, lambda msg=err_msg: (
                    self._clear_active_capture_styles(),
                    self._messagebox_error("Error", f"Capture failed: {msg}")
                ))

        threading.Thread(target=run, daemon=True).start()

    # ---------------------------------------------------------------------
    # Cooler controls + telemetry
    # ---------------------------------------------------------------------
    def _cooler_target_c(self) -> int:
        try:
            t = int(float(self.target_temp_var.get()))
        except Exception:
            t = int(self.settings.get("target_temp_c", -10))
        return int(max(min(t, Config.COOLER_MAX), Config.COOLER_MIN))

    def _init_cooler_from_settings(self):
        """Best-effort re-apply persisted cooler state after startup."""
        try:
            if not hasattr(self, "cooler_var"):
                return
            if bool(self.cooler_var.get()):
                self.oz.camera.cooler(True, self._cooler_target_c())
                self.set_status(f"Cooler ON (target {self._cooler_target_c()} °C)")
        except Exception:
            # Don't spam popups on startup; telemetry will still run.
            pass

    def _on_cooler_toggle(self, *_):
        try:
            on = bool(self.cooler_var.get())
            target = self._cooler_target_c()
            if on:
                self.oz.camera.cooler(True, target)
                self.set_status(f"Cooler ON (target {target} °C)")
            else:
                self.oz.camera.cooler(False, None)
                self.set_status("Cooler OFF")
            self._save_settings()
        except Exception as e:
            self._messagebox_error("Error", f"Cooler error: {e}")

    def _on_target_temp_commit(self, *_):
        try:
            # Clamp + store
            self.target_temp_var.set(self._cooler_target_c())
            self._save_settings()
            # If cooler is ON, apply immediately
            if bool(self.cooler_var.get()):
                self.oz.camera.cooler(True, self._cooler_target_c())
                self.set_status(f"Cooler target set to {self._cooler_target_c()} °C")
        except Exception as e:
            self._messagebox_error("Error", f"Target temperature error: {e}")

    def _poll_camera_telemetry(self):
        """Update sensor temperature and cooler power labels (best-effort)."""
        try:
            cam = getattr(self.oz, "camera", None)
            if asi is not None and cam is not None:
                # ASI_TEMPERATURE is typically reported in 0.1°C units.
                raw_t = None
                raw_p = None
                try:
                    raw_t = cam.get_control_value_safe(asi.ASI_TEMPERATURE)
                except Exception:
                    raw_t = None
                try:
                    raw_p = cam.get_control_value_safe(getattr(asi, "ASI_COOLER_POWER_PERC", None))
                except Exception:
                    raw_p = None

                if hasattr(self, "temp_now_lbl"):
                    if raw_t is None:
                        self.temp_now_lbl.config(text="— °C")
                    else:
                        self.temp_now_lbl.config(text=f"{raw_t / 10.0:.1f} °C")
                if hasattr(self, "cooler_pwr_lbl"):
                    if raw_p is None:
                        self.cooler_pwr_lbl.config(text="— %")
                    else:
                        self.cooler_pwr_lbl.config(text=f"{int(raw_p)} %")
        except Exception:
            pass
        finally:
            # Reschedule unconditionally so the loop never silently dies
            try:
                self.root.after(1000, self._poll_camera_telemetry)
            except Exception:
                pass

    # Back-compat: some older UI flows called toggle_cooler() directly.
    def toggle_cooler(self):
        self._on_cooler_toggle()
    
    def capture_burst(self):
        """Capture N images using the current selections (same settings)."""
        try:
            exp = self.exp_entry.get().strip()
            if not exp:
                self._messagebox_error("Error", "Experiment name is required.")
                return
            if not self._selected_led_labels():
                self._messagebox_error("Error", "Please select at least one LED wavelength before capturing.")
                return
            if not self._selected_en_labels():
                self._messagebox_error("Error", "Please enable at least one section before capturing.")
                return

            try:
                n = int(getattr(self, "burst_count_var", tb.IntVar(value=1)).get())
            except Exception:
                n = 1
            if n <= 0:
                self._messagebox_error("Error", "Repeat count must be >= 1.")
                return

            paths = self.oz.capture_burst(
                experiment=exp,
                exposure_us=self._exposure_us(),
                gain=self._gain(),
                selected_led_labels=self._selected_led_labels(),
                selected_en_labels=self._selected_en_labels(),
                intensity_pct=self._intensity_pct(),
                count=n,
                comments=self.comment_text.get("1.0", "end").strip(),
            )

            if paths:
                self.set_status(f"Saved burst: {len(paths)} images (last: {paths[-1]})")
            else:
                self.set_status("Burst stopped (0 images saved).")
        except Exception as e:
            self._messagebox_error("Error", f"Burst capture failed: {e}")
    
    # =========================================================================
    #  Timelapse
    # =========================================================================
    def toggle_timelapse(self):
        if not self.timelapse_running:
            try:
                delay_s = float(self.tl_delay.get()) * 60.0
                duration_s = float(self.tl_duration.get()) * 60.0
            except Exception:
                self._messagebox_error("Error", "Invalid timelapse inputs.")
                return
            if duration_s <= 0 or delay_s <= 0:
                self._messagebox_error("Error", "Delay and Duration must be > 0.")
                return
            # Snapshot timelapse-specific gain and exposure
            try:
                tl_gain = int(float(self.tl_gain_entry.get()))
                tl_gain = int(max(min(tl_gain, Config.GAIN_MAX), Config.GAIN_MIN))
            except Exception:
                tl_gain = 100
            try:
                tl_exp_val = float(self.tl_exposure_entry.get())
                tl_exp_mult = Config.UNIT_CONVERSION[self.tl_exposure_unit_var.get()]
                tl_exposure_us = int(max(min(tl_exp_val * tl_exp_mult, Config.EXPOSURE_MAX), Config.EXPOSURE_MIN))
            except Exception:
                tl_exposure_us = 1000
            self.timelapse_running = True
            self._tl_total_captures = max(1, int(duration_s / delay_s))
            self.tl_btn.config(text="Stop Timelapse", bootstyle="danger")
            self.tl_progress.configure(value=0, maximum=self._tl_total_captures)
            self._set_active_capture_styles()
            self._start_timelapse_thread(delay_s, duration_s, tl_gain, tl_exposure_us)
        else:
            self._stop_timelapse()
    
    def _start_timelapse_thread(self, delay_s: float, duration_s: float,
                               tl_gain: int = 100, tl_exposure_us: int = 1000):
        self._tl_counter = 0
        def run():
            start = time.time()
            next_shot = start
            while self.timelapse_running:
                now = time.time()
                elapsed = now - start
                remaining = max(0.0, duration_s - elapsed)
                self.root.after(0, lambda e=elapsed, r=remaining: self._update_tl_ui(e, r, duration_s))
                if elapsed >= duration_s:
                    break
                if now >= next_shot:
                    self._tl_counter += 1
                    counter = self._tl_counter
                    self.root.after(0, lambda c=counter: self.capture_one(
                        tl_index=c,
                        gain_override=tl_gain,
                        exposure_us_override=tl_exposure_us,
                    ))
                    next_shot = now + delay_s
                time.sleep(0.1)
            self.root.after(0, self._finish_timelapse_ui)
        self._tl_thread = threading.Thread(target=run, daemon=True)
        self._tl_thread.start()
    
    def _update_tl_ui(self, _elapsed_s: float, remaining_s: float, _total_s: float):
        total_tl = getattr(self, "_tl_total_captures", 1)
        n = getattr(self, "_tl_counter", 0)
        self.tl_progress.configure(value=min(n, total_tl))
        mins, secs = divmod(int(remaining_s), 60)
        self.tl_status.config(text=f"Capture {n}/{total_tl} — remaining {mins:02d}:{secs:02d}")
    
    def _finish_timelapse_ui(self):
        self.timelapse_running = False
        self.tl_btn.config(text="Capture Timelapse", bootstyle="success")
        self.tl_status.config(text="Timelapse complete.")
        self.tl_progress.configure(value=self.tl_progress["maximum"])
        self.set_status("Timelapse complete.")
        self._clear_active_capture_styles()
    
    def _stop_timelapse(self):
        self.timelapse_running = False
        self.tl_status.config(text="Stopping…")
    
    # =========================================================================
    #  Automatic loop thread control
    # =========================================================================
    def toggle_auto_run(self):
        if self.first_sweep_start_time is None:
            self.first_sweep_start_time = time.time()

        self.sweep_number += 1
        
        # Stop if already running
        if getattr(self, "_auto_thread", None) and self._auto_thread.is_alive():
            self.auto_stop_requested = True
            self.auto_status.config(text="Stopping…")
            self.auto_btn.config(state="disabled")
            return
        # Parse inputs
        try:
            experiment = self.exp_entry.get().strip() or "experiment"
            settle_ms = max(0.0, float(self.auto_settle_entry.get()))
        except Exception:
            self._messagebox_error("Error", "Invalid run inputs.")
            return
        # Read sweep settings
        sweep_wls = self._sweep_wavelengths()
        if not sweep_wls:
            # Fall back to LED panel selection
            sweep_wls = self._selected_led_labels()
        if not sweep_wls:
            self._messagebox_error("Error", "Please select at least one wavelength in the Sweep panel or LED panel.")
            return
        sweep_ints = self._sweep_intensities() or [100]
        sweep_gains = self._sweep_gains()
        per_wl_mode = self.sweep_exp_mode.get() == "per_wl"
        sweep_exps = None
        sweep_exps_per_wl = None
        if per_wl_mode:
            sweep_exps_per_wl = self._sweep_exposures_per_wl()
        else:
            sweep_exps = self._sweep_exposures_us() or [self._exposure_us()]
        # Calculate total before run
        try:
            total = self.oz.compute_total_images(
                selected_led_labels=sweep_wls,
                selected_en_labels=self._selected_en_labels(),
                intensities=sweep_ints,
                exposures_us=sweep_exps,
                exposures_per_wl=sweep_exps_per_wl,
                intensities_per_wl=self._sweep_intensities_per_wl() if per_wl_mode else None,
                exposures_per_wl_multi=self._sweep_exposures_per_wl_multi() if per_wl_mode else None,
                intensities_per_wl_multi=self._sweep_intensities_per_wl_multi() if per_wl_mode else None,
                gains=sweep_gains,
                include_dark=self.include_dark_var.get(),
            )
        except Exception as e:
            self._messagebox_error("Error", f"Failed to calculate total: {e}")
            return

        # Prep UI
        self.auto_stop_requested = False
        self.auto_btn.config(text="Stop Automatic Run", bootstyle="danger")
        self.auto_status.config(text=f"Starting… Total images: {total:,}")
        self._set_active_capture_styles()
        self.auto_prog.configure(value=0, maximum=total)
        self.auto_total_lbl.config(text=f"Total images: {total:,}")
        self.auto_eta_lbl.config(text="  |  ETA: —")
        self.auto_start_time = time.time()
        
        def should_stop():
            return self.auto_stop_requested
        
        def _fmt_eta(seconds: float) -> str:
            if seconds <= 0 or not (seconds < 10 * 365 * 24 * 3600):
                return "—"
            m, s = divmod(int(seconds), 60)
            h, m = divmod(m, 60)
            if h > 0:
                return f"{h:d}:{m:02d}:{s:02d}"
            else:
                return f"{m:02d}:{s:02d}"
        
        def progress_cb(done: int, total_count: int, status: str, fpath: str | None = None, extra_meta: dict = None):
            if fpath:
                try:
                    self._log_capture_metadata(fpath, experiment_override=experiment, extra_meta=extra_meta)
                except Exception:
                    pass
            def ui_update():
                self.auto_prog.configure(maximum=total_count, value=done)
                elapsed = max(1e-6, time.time() - self.auto_start_time)
                avg_per = elapsed / max(1, done)
                remaining = max(0, total_count - done)
                eta_s = remaining * avg_per
                self.auto_eta_lbl.config(text=f"  |  ETA: {_fmt_eta(eta_s)}")
                self.auto_status.config(text=f"{status}  ({done}/{total_count})")
                if fpath:
                    try:
                        self._update_preview(fpath)
                    except Exception:
                        pass
                if done >= total_count:
                    self.auto_btn.config(text="Start Sweep", bootstyle="primary", state="normal")
                    self.set_status("Sweep complete.")
            self.root.after(0, ui_update)
        
        def run():
            try:
                self.oz.auto_loop(
                    experiment=experiment,
                    gain=sweep_gains[0],
                    settle_s=settle_ms / 1000.0,
                    progress_cb=progress_cb,
                    should_stop=should_stop,
                    selected_led_labels=sweep_wls,
                    selected_en_labels=self._selected_en_labels(),
                    intensities=sweep_ints,
                    exposures_us=sweep_exps,
                    exposures_per_wl=sweep_exps_per_wl,
                    intensities_per_wl=self._sweep_intensities_per_wl() if per_wl_mode else None,
                    intensities_per_wl_multi=self._sweep_intensities_per_wl_multi() if per_wl_mode else None,
                    exposures_per_wl_multi=self._sweep_exposures_per_wl_multi() if per_wl_mode else None,
                    gains=sweep_gains,
                    gains_per_wl_multi=self._sweep_gains_per_wl_multi() if per_wl_mode else None,
                    comments=self.comment_text.get("1.0", "end").strip(),
                    include_dark=self.include_dark_var.get(),
                )
            except Exception as e:
                self.root.after(0, lambda: self._messagebox_error("Error", f"Auto run failed: {e}"))
            finally:
                # Kick off analysis on the sweep folder (non-blocking)
                if not self.auto_stop_requested:
                    sweep_folder = os.path.join(self.save_dir, experiment)
                    self._run_analysis_async(sweep_folder, experiment)
                def finish():
                    self.auto_btn.config(text="Start Automatic Run", bootstyle="primary", state="normal")
                    if self.auto_stop_requested:
                        self.auto_status.config(text="Stopped.")
                        self.set_status("Automatic run stopped.")
                    self._clear_active_capture_styles()
                self.root.after(0, finish)
        
        self._auto_thread = threading.Thread(target=run, daemon=True)
        self._auto_thread.start()

    # =========================================================================
    #  Multi-Day Run
    # =========================================================================
    def toggle_multiday_run(self):
        if self.md_running:
            self.md_stop_requested = True
            self.md_status.config(text="Stopping…")
            self.md_btn.config(state="disabled")
            return

        try:
            interval_h = float(self.md_interval_entry.get())
            total_days = float(self.md_days_entry.get())
        except Exception:
            self._messagebox_error("Error", "Invalid multi-day inputs.")
            return

        if interval_h <= 0 or total_days <= 0:
            self._messagebox_error("Error", "Interval and duration must be > 0.")
            return

        exp = self.exp_entry.get().strip() or "experiment"
        import math
        total_captures = max(1, math.floor((total_days * 24) / interval_h))

        self.md_running = True
        self.md_stop_requested = False
        self.md_btn.config(text="Stop Multi-Day Sweep", bootstyle="danger")
        self.md_prog.configure(value=0, maximum=total_captures)
        self.md_status.config(
            text=f"Scheduled — {total_captures} sweep(s) over {total_days:.0f} day(s).")

        def run():
            import datetime as dt
            done = 0
            interval_td = dt.timedelta(hours=interval_h)
            start_time = dt.datetime.now()
            next_fire = start_time  # fire first sweep immediately

            while not self.md_stop_requested:
                while dt.datetime.now() < next_fire:
                    if self.md_stop_requested:
                        break
                    time.sleep(5)

                if self.md_stop_requested:
                    break

                done += 1
                if done > total_captures:
                    break

                time_label = dt.datetime.now().strftime("%Y%m%d_%H-%M-%S")
                day_exp = f"{exp}_{time_label}_sweep{done:03d}"

                self.root.after(0, lambda d=done: (
                    self.md_prog.configure(value=d),
                    self.md_status.config(text=f"Capturing sweep {d}/{total_captures}…"),
                ))
                self.root.after(0, self._set_active_capture_styles)

                try:
                    wls = self._sweep_wavelengths() or self._selected_led_labels()
                    settle_ms = max(0.0, float(self.auto_settle_entry.get()))
                    sweep_ints = self._sweep_intensities() or [100]
                    md_gains = self._sweep_gains()
                    md_per_wl = self.sweep_exp_mode.get() == "per_wl"
                    user_comments = self.comment_text.get("1.0", "end").strip()

                    def md_progress_cb(d, total_count, status, fpath=None, extra_meta=None, _exp=day_exp):
                        if fpath:
                            try:
                                self._log_capture_metadata(fpath, experiment_override=_exp, extra_meta=extra_meta)
                            except Exception:
                                pass
                        def _md_ui(d=d, total_count=total_count, status=status, fpath=fpath):
                            self.auto_prog.configure(maximum=total_count, value=d)
                            self.auto_status.config(text=f"{status}  ({d}/{total_count})")
                            if fpath:
                                try:
                                    self._update_preview(fpath)
                                except Exception:
                                    pass
                        self.root.after(0, _md_ui)

                    self.oz.auto_loop(
                        experiment=day_exp,
                        gain=md_gains[0],
                        settle_s=settle_ms / 1000.0,
                        selected_led_labels=wls,
                        selected_en_labels=self._selected_en_labels(),
                        intensities=sweep_ints,
                        exposures_us=None if md_per_wl else (self._sweep_exposures_us() or [self._exposure_us()]),
                        exposures_per_wl=self._sweep_exposures_per_wl() if md_per_wl else None,
                        intensities_per_wl=self._sweep_intensities_per_wl() if md_per_wl else None,
                        intensities_per_wl_multi=self._sweep_intensities_per_wl_multi() if md_per_wl else None,
                        exposures_per_wl_multi=self._sweep_exposures_per_wl_multi() if md_per_wl else None,
                        gains=md_gains,
                        gains_per_wl_multi=self._sweep_gains_per_wl_multi() if md_per_wl else None,
                        comments=user_comments,
                        should_stop=lambda: self.md_stop_requested,
                        progress_cb=md_progress_cb,
                        include_dark=self.include_dark_var.get(),
                    )
                except Exception as e:
                    self.root.after(0, lambda err=str(e):
                        self.set_status(f"Multi-day sweep error: {err}"))

                self.root.after(0, self._clear_active_capture_styles)

                self.root.after(0, lambda d=done: (
                    self.md_prog.configure(value=d),
                    self.md_status.config(
                        text=f"Sweep {d}/{total_captures} done. {total_captures - d} remaining."),
                ))

                # Analyze the completed sweep folder in background
                sweep_folder = os.path.join(self.save_dir, day_exp)
                self._run_analysis_async(sweep_folder, day_exp)

                next_fire = dt.datetime.now() + interval_td

            def finish():
                self.md_running = False
                self.md_btn.config(
                    text="Start Multi-Day Sweep", bootstyle="warning", state="normal")
                if self.md_stop_requested:
                    self.md_status.config(text="Stopped.")
                    self.set_status("Multi-day sweep stopped.")
                else:
                    self.md_prog.configure(value=total_captures)
                    self.md_status.config(text="Multi-day sweep complete.")
                    self.set_status("Multi-day sweep complete.")
            self.root.after(0, finish)

        self._md_thread = threading.Thread(target=run, daemon=True)
        self._md_thread.start()

    # =========================================================================
    #  Status bar (bottom)
    # =========================================================================
    def _make_statusbar(self, parent):
        import ttkbootstrap as tb
        import tkinter as tk

        bar = tb.Frame(parent, bootstyle="secondary")
        bar.pack(side=tk.BOTTOM, fill=tk.X)

        self._status_var = tb.StringVar(value="")
        self._status_lbl = tb.Label(bar, textvariable=self._status_var, anchor="w")
        self._status_lbl.pack(side=tk.LEFT, padx=8, pady=4)

        try:
            v = getattr(self, "app_version", None)
            if v is not None:
                tb.Label(bar, text=f"v{v}", bootstyle="secondary").pack(side=tk.RIGHT, padx=8)
        except Exception:
            pass

    def set_status(self, text: str):
        # safe to call from any thread
        self.root.after(0, lambda: self._status_var.set(text))


    # =========================================================================
    #  Post-sweep analysis
    # =========================================================================
    def _run_analysis_async(self, sweep_folder: str, label: str):
        """Run analyze_images.analyze() on sweep_folder in a background thread."""
        if not _HAS_ANALYZE:
            return
        if not self.auto_analyze_var.get():
            return

        def run():
            out_folder = os.path.join(sweep_folder, "analysis")
            self.root.after(0, lambda: self.set_status(f"Analyzing {label}…"))
            try:
                _analyze_images(sweep_folder, out_folder, sat_fraction=0.001)
                self.root.after(0, lambda: self.set_status(f"Analysis complete: {label}"))
            except Exception as e:
                self.root.after(0, lambda err=str(e): self.set_status(f"Analysis error: {err}"))

        threading.Thread(target=run, daemon=True).start()

    # =========================================================================
    #  App close
    # =========================================================================
    def on_close(self):
        self.timelapse_running = False
        self.auto_stop_requested = True
        self._save_settings()
        try:
            self.root.destroy()
        except Exception:
            pass


