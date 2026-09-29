# Development note: The main application functions, supporting utilities, and
# debugging code in this script were developed with AI assistance. The author
# reviewed the code and remains responsible for its use and validation.

import os
import sys
import time
import numpy as np
from PIL import Image, ImageTk

import zwoasi as asi
import tkinter as tk
from tkinter import ttk, messagebox, filedialog

# Configure this path for the installed ZWO ASI SDK library.
ASI_LIB_PATH = r"/Users/bardiya/Downloads/ASI_Camera_SDK/ASI_linux_mac_SDK_V1.40/lib/mac_arm64/libASICamera2.dylib"


class ASIControlApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("ZWO ASI Live View & Controls")

        # ZWO ASI camera and capture state.
        self.camera = None
        self.cameras_found = []
        self.live_running = False

        # Captured-frame and display-image state.
        self.last_frame = None          # RGB NumPy array captured from the camera.
        self.adjusted_pil = None        # PIL image after display adjustments.
        self.tk_image = None            # Tk image retained for canvas rendering.

        # Zoom and pan state.
        self.zoom_factor = 1.0
        self.min_zoom = 0.5             # Permit zooming out beyond the fitted view.
        self.max_zoom = 5.0
        self.base_scale = None          # Scale required to fit the image in the canvas.
        self.last_canvas_size = (None, None)
        self.last_offset_x = 0
        self.last_offset_y = 0
        self.last_display_scale = 1.0
        self.drag_mode = None           # Active drag operation: "pan", "circle", or None.

        # Convert exposure-control units to microseconds for the camera SDK.
        self.exposure_modes = {
            "µs": 1,
            "ms": 1000,
            "s": 1_000_000,
        }

        self.exposure_ranges = {
            "µs": (50, 1_000_000),  # 50 µs to 1 s.
            "ms": (1, 5000),        # 1 ms to 5 s.
            "s": (1, 2000),         # 1 s to approximately 33 minutes.
        }

        # Camera-control limits, updated after a camera is opened.
        self.gain_min = 0
        self.gain_max = 450
        self.exposure_min_us = 32
        self.exposure_max_us = 2_000_000_000

        # Display-adjustment state; 1.0 is neutral for each setting.
        self.brightness = 1.0
        self.contrast = 1.0
        self.gamma = 1.0
        self.saturation = 1.0

        # Main crosshair state.
        self.crosshair_thickness = 1.0
        self.crosshair_enabled_var = tk.BooleanVar(value=True)

        # Red-circle state in image-pixel coordinates.
        self.circle_center_x = None
        self.circle_center_y = None
        self.circle_radius_px = 50.0
        self.circle_enabled_var = tk.BooleanVar(value=False)
        self.circle_move_var = tk.BooleanVar(value=False)

        # Optional crosshair centered on the red circle.
        self.circle_crosshair_enabled_var = tk.BooleanVar(value=False)

        # Optional yellow and purple circles centered on the red circle.
        self.extra_circles_enabled_var = tk.BooleanVar(value=False)

        self.yellow_show_var = tk.BooleanVar(value=False)
        self.yellow_radius_px = 20.0

        self.purple_show_var = tk.BooleanVar(value=False)
        self.purple_radius_px = 30.0

        # Live-view update interval; 50 ms corresponds to at most about 20 FPS.
        self.frame_interval_ms = 50

        # Frame-rate tracking state.
        self.last_frame_time = None
        self.current_fps = 0.0

        self._build_ui()
        self._init_zwo()
        self._populate_cameras()

    # ==================== ZWO ASI CAMERA ====================

    def _init_zwo(self):
        if not os.path.exists(ASI_LIB_PATH):
            messagebox.showerror(
                "ASI library not found",
                f"Please set ASI_LIB_PATH correctly.\nCurrent: {ASI_LIB_PATH}",
            )
            self.root.destroy()
            return

        try:
            asi.init(ASI_LIB_PATH)
        except Exception as e:
            messagebox.showerror(
                "ASI init error",
                f"Could not initialize ASI SDK:\n{e}",
            )
            self.root.destroy()
            return

    def _populate_cameras(self):
        try:
            num_cams = asi.get_num_cameras()
        except Exception as e:
            messagebox.showerror("ASI error", f"Error listing cameras:\n{e}")
            self.root.destroy()
            return

        if num_cams == 0:
            messagebox.showerror("No cameras", "No ZWO ASI cameras detected.")
            self.root.destroy()
            return

        self.cameras_found = asi.list_cameras()
        self.camera_combo["values"] = self.cameras_found

        if self.cameras_found:
            self.camera_combo.current(0)
            self._on_camera_selected()

    def _open_camera(self, camera_index: int):
        # Stop and release any previously opened camera.
        self._stop_live()
        if self.camera is not None:
            try:
                self.camera.stop_video_capture()
            except Exception:
                pass
            try:
                self.camera.close()
            except Exception:
                pass
            self.camera = None

        if camera_index < 0 or camera_index >= len(self.cameras_found):
            return

        try:
            self.camera = asi.Camera(camera_index)
        except Exception as e:
            messagebox.showerror("Camera error", f"Could not open camera:\n{e}")
            return

        # Configure a full-frame RGB24 region of interest.
        try:
            props = self.camera.get_camera_property()
            width = props["MaxWidth"]
            height = props["MaxHeight"]
        except Exception:
            width, height = 1280, 720

        try:
            self.camera.set_roi(width=width, height=height, bins=1,
                                image_type=asi.ASI_IMG_RGB24)
        except Exception as e:
            messagebox.showerror("ROI error", f"Could not set ROI:\n{e}")
            return

        # Disable automatic exposure and gain limits when supported.
        try:
            self.camera.set_control_value(asi.ASI_AUTO_MAX_EXP, 0)
            self.camera.set_control_value(asi.ASI_AUTO_MAX_GAIN, 0)
        except Exception:
            pass

        # Read hardware-specific control limits when available.
        try:
            controls = self.camera.get_controls()
            if "Gain" in controls:
                self.gain_min = controls["Gain"]["MinValue"]
                self.gain_max = controls["Gain"]["MaxValue"]
            if "Exposure" in controls:
                self.exposure_min_us = controls["Exposure"]["MinValue"]
                self.exposure_max_us = controls["Exposure"]["MaxValue"]
        except Exception:
            pass

        # Configure the gain controls from the camera's supported range.
        self.gain_scale.config(from_=self.gain_min, to=self.gain_max)
        mid_gain = (self.gain_min + self.gain_max) / 2
        self.gain_scale.set(mid_gain)
        self.gain_entry.delete(0, tk.END)
        self.gain_entry.insert(0, f"{mid_gain:.1f}")

        # Initialize the exposure controls.
        self._update_exposure_slider_range()
        self.exposure_scale.set(10.0)
        self.exposure_entry.delete(0, tk.END)
        self.exposure_entry.insert(0, "10")

        self._apply_gain()
        self._apply_exposure()

        # Reset the zoom and viewport state.
        self.zoom_factor = 1.0
        self.base_scale = None
        self.last_canvas_size = (None, None)
        self.canvas.xview_moveto(0)
        self.canvas.yview_moveto(0)

        # Reset the frame-rate display.
        self.last_frame_time = None
        self.current_fps = 0.0
        self.fps_label.config(text="FPS: 0.0")

        # Initialize the circle at the image center with a size-dependent radius.
        self.circle_center_x = width / 2.0
        self.circle_center_y = height / 2.0

        default_radius = min(width, height) / 4.0
        self.circle_radius_px = default_radius

        self.circle_radius_scale.config(from_=5.0, to=float(min(width, height)))
        self.circle_radius_scale.set(default_radius)
        self.circle_radius_entry.delete(0, tk.END)
        self.circle_radius_entry.insert(0, f"{default_radius:.1f}")

        # Initialize the auxiliary-circle radii relative to the red circle.
        self.yellow_radius_px = default_radius * 0.5
        self.purple_radius_px = default_radius * 0.8

        self._update_extra_circle_limits()

        # Start live video capture.
        try:
            self.camera.start_video_capture()
        except Exception as e:
            messagebox.showerror("Video error", f"Could not start video capture:\n{e}")
            return

        self.live_running = True
        self._update_frame()

    # ==================== USER INTERFACE ====================

    def _build_ui(self):
        # Build the top camera-control bar.
        top = ttk.Frame(self.root, padding=5)
        top.pack(side=tk.TOP, fill=tk.X)

        ttk.Label(top, text="Camera:").pack(side=tk.LEFT)
        self.camera_combo = ttk.Combobox(top, state="readonly", width=30)
        self.camera_combo.pack(side=tk.LEFT, padx=5)
        self.camera_combo.bind("<<ComboboxSelected>>", lambda e: self._on_camera_selected())

        self.live_button = ttk.Button(top, text="Start Live", command=self._toggle_live)
        self.live_button.pack(side=tk.LEFT, padx=5)

        self.capture_button = ttk.Button(top, text="Save current frame", command=self._save_frame)
        self.capture_button.pack(side=tk.LEFT, padx=5)

        # Display the current frame rate and camera temperature.
        self.fps_label = ttk.Label(top, text="FPS: 0.0")
        self.fps_label.pack(side=tk.LEFT, padx=10)

        self.temp_label = ttk.Label(top, text="Temp: -- °C")
        self.temp_label.pack(side=tk.LEFT)

        # Split the window into controls and a live preview.
        main = ttk.Frame(self.root)
        main.pack(fill=tk.BOTH, expand=True)

        # Build the scrollable control panel on the left.
        settings_frame = ttk.Frame(main)
        settings_frame.pack(side=tk.LEFT, fill=tk.Y)

        # Place the settings controls in a vertically scrollable canvas.
        self.settings_canvas = tk.Canvas(settings_frame, borderwidth=0, highlightthickness=0)
        settings_scrollbar = ttk.Scrollbar(settings_frame, orient=tk.VERTICAL,
                                           command=self.settings_canvas.yview)
        self.settings_canvas.configure(yscrollcommand=settings_scrollbar.set)

        self.settings_canvas.pack(side=tk.LEFT, fill=tk.Y, expand=False)
        settings_scrollbar.pack(side=tk.RIGHT, fill=tk.Y)

        # Use an inner frame as the canvas window for the controls.
        controls = ttk.Frame(self.settings_canvas, padding=5)
        self.settings_canvas.create_window((0, 0), window=controls, anchor="nw")

        def _on_controls_configure(event):
            # Keep the scrollable region synchronized with the inner frame.
            self.settings_canvas.configure(scrollregion=self.settings_canvas.bbox("all"))

        controls.bind("<Configure>", _on_controls_configure)

        # Set a practical fixed width for the settings panel.
        self.settings_canvas.config(width=320)

        # Exposure controls.
        ttk.Label(controls, text="Exposure mode:").pack(anchor="w")
        self.exposure_mode_var = tk.StringVar(value="ms")
        self.exposure_mode_combo = ttk.Combobox(
            controls,
            state="readonly",
            values=list(self.exposure_modes.keys()),
            textvariable=self.exposure_mode_var,
            width=5,
        )
        self.exposure_mode_combo.pack(anchor="w", pady=(0, 10))
        self.exposure_mode_combo.bind("<<ComboboxSelected>>",
                                      lambda e: self._on_exposure_mode_changed())

        exposure_row = ttk.Frame(controls)
        exposure_row.pack(fill=tk.X, pady=(0, 2))
        self.exposure_label = ttk.Label(exposure_row, text="Exposure:")
        self.exposure_label.pack(side=tk.LEFT)
        self.exposure_value_label = ttk.Label(exposure_row, text="0 ms")
        self.exposure_value_label.pack(side=tk.LEFT, padx=(5, 0))

        entry_row = ttk.Frame(controls)
        entry_row.pack(fill=tk.X, pady=(0, 5))
        ttk.Label(entry_row, text="Set exposure:").pack(side=tk.LEFT)
        self.exposure_entry = ttk.Entry(entry_row, width=10)
        self.exposure_entry.pack(side=tk.LEFT, padx=(5, 0))
        self.exposure_entry.insert(0, "10")
        self.exposure_entry.bind("<Return>", lambda e: self._on_exposure_entry())
        self.exposure_entry.bind("<FocusOut>", lambda e: self._on_exposure_entry())

        self.exposure_scale = ttk.Scale(
            controls,
            from_=1,
            to=1000,
            orient=tk.HORIZONTAL,
            command=lambda v: self._on_exposure_slider_changed(v),
        )
        self.exposure_scale.pack(fill=tk.X, pady=(0, 15))

        # Gain controls.
        ttk.Label(controls, text="Gain:").pack(anchor="w")

        gain_row = ttk.Frame(controls)
        gain_row.pack(fill=tk.X, pady=(0, 2))
        self.gain_value_label = ttk.Label(gain_row, text="0")
        self.gain_value_label.pack(side=tk.LEFT)

        gain_entry_row = ttk.Frame(controls)
        gain_entry_row.pack(fill=tk.X, pady=(0, 5))
        ttk.Label(gain_entry_row, text="Set gain:").pack(side=tk.LEFT)
        self.gain_entry = ttk.Entry(gain_entry_row, width=10)
        self.gain_entry.pack(side=tk.LEFT, padx=(5, 0))
        self.gain_entry.insert(0, "0")
        self.gain_entry.bind("<Return>", lambda e: self._on_gain_entry())
        self.gain_entry.bind("<FocusOut>", lambda e: self._on_gain_entry())

        self.gain_scale = ttk.Scale(
            controls,
            from_=0,
            to=450,
            orient=tk.HORIZONTAL,
            command=lambda v: self._on_gain_slider_changed(v),
        )
        self.gain_scale.pack(fill=tk.X, pady=(0, 15))

        # Display-adjustment controls.
        ttk.Label(controls, text="Display adjustments").pack(anchor="w", pady=(10, 0))

        # Brightness controls.
        b_row = ttk.Frame(controls)
        b_row.pack(fill=tk.X, pady=(2, 0))
        ttk.Label(b_row, text="Brightness:").pack(side=tk.LEFT)
        self.brightness_entry = ttk.Entry(b_row, width=6)
        self.brightness_entry.pack(side=tk.LEFT, padx=(5, 0))
        self.brightness_entry.insert(0, "1.0")
        self.brightness_entry.bind("<Return>", lambda e: self._on_brightness_entry())
        self.brightness_entry.bind("<FocusOut>", lambda e: self._on_brightness_entry())

        self.brightness_scale = ttk.Scale(
            controls,
            from_=0.1,
            to=3.0,
            orient=tk.HORIZONTAL,
            command=lambda v: self._on_brightness_slider_changed(v),
        )
        self.brightness_scale.set(1.0)
        self.brightness_scale.pack(fill=tk.X)

        # Contrast controls.
        c_row = ttk.Frame(controls)
        c_row.pack(fill=tk.X, pady=(8, 0))
        ttk.Label(c_row, text="Contrast:").pack(side=tk.LEFT)
        self.contrast_entry = ttk.Entry(c_row, width=6)
        self.contrast_entry.pack(side=tk.LEFT, padx=(5, 0))
        self.contrast_entry.insert(0, "1.0")
        self.contrast_entry.bind("<Return>", lambda e: self._on_contrast_entry())
        self.contrast_entry.bind("<FocusOut>", lambda e: self._on_contrast_entry())

        self.contrast_scale = ttk.Scale(
            controls,
            from_=0.1,
            to=3.0,
            orient=tk.HORIZONTAL,
            command=lambda v: self._on_contrast_slider_changed(v),
        )
        self.contrast_scale.set(1.0)
        self.contrast_scale.pack(fill=tk.X)

        # Gamma controls.
        g_row = ttk.Frame(controls)
        g_row.pack(fill=tk.X, pady=(8, 0))
        ttk.Label(g_row, text="Gamma:").pack(side=tk.LEFT)
        self.gamma_entry = ttk.Entry(g_row, width=6)
        self.gamma_entry.pack(side=tk.LEFT, padx=(5, 0))
        self.gamma_entry.insert(0, "1.0")
        self.gamma_entry.bind("<Return>", lambda e: self._on_gamma_entry())
        self.gamma_entry.bind("<FocusOut>", lambda e: self._on_gamma_entry())

        self.gamma_scale = ttk.Scale(
            controls,
            from_=0.1,
            to=3.0,
            orient=tk.HORIZONTAL,
            command=lambda v: self._on_gamma_slider_changed(v),
        )
        self.gamma_scale.set(1.0)
        self.gamma_scale.pack(fill=tk.X)

        # Saturation controls.
        s_row = ttk.Frame(controls)
        s_row.pack(fill=tk.X, pady=(8, 0))
        ttk.Label(s_row, text="Saturation:").pack(side=tk.LEFT)
        self.saturation_entry = ttk.Entry(s_row, width=6)
        self.saturation_entry.pack(side=tk.LEFT, padx=(5, 0))
        self.saturation_entry.insert(0, "1.0")
        self.saturation_entry.bind("<Return>", lambda e: self._on_saturation_entry())
        self.saturation_entry.bind("<FocusOut>", lambda e: self._on_saturation_entry())

        self.saturation_scale = ttk.Scale(
            controls,
            from_=0.0,
            to=3.0,
            orient=tk.HORIZONTAL,
            command=lambda v: self._on_saturation_slider_changed(v),
        )
        self.saturation_scale.set(1.0)
        self.saturation_scale.pack(fill=tk.X)

        # Main-crosshair controls.
        ttk.Label(controls, text="Main crosshair").pack(anchor="w", pady=(10, 0))
        ch_row1 = ttk.Frame(controls)
        ch_row1.pack(fill=tk.X, pady=(2, 0))
        self.crosshair_check = ttk.Checkbutton(
            ch_row1,
            text="Show crosshair",
            variable=self.crosshair_enabled_var,
            command=self._render_current_frame,
        )
        self.crosshair_check.pack(side=tk.LEFT)

        ch_row2 = ttk.Frame(controls)
        ch_row2.pack(fill=tk.X, pady=(2, 0))
        ttk.Label(ch_row2, text="Thickness:").pack(side=tk.LEFT)
        self.crosshair_entry = ttk.Entry(ch_row2, width=6)
        self.crosshair_entry.pack(side=tk.LEFT, padx=(5, 0))
        self.crosshair_entry.insert(0, "1.0")
        self.crosshair_entry.bind("<Return>", lambda e: self._on_crosshair_entry())
        self.crosshair_entry.bind("<FocusOut>", lambda e: self._on_crosshair_entry())

        self.crosshair_scale = ttk.Scale(
            controls,
            from_=1.0,
            to=10.0,
            orient=tk.HORIZONTAL,
            command=lambda v: self._on_crosshair_slider_changed(v),
        )
        self.crosshair_scale.set(1.0)
        self.crosshair_scale.pack(fill=tk.X)

        # Red-circle controls.
        ttk.Label(controls, text="Circle overlay").pack(anchor="w", pady=(10, 0))

        circle_row1 = ttk.Frame(controls)
        circle_row1.pack(fill=tk.X, pady=(2, 0))

        self.circle_check = ttk.Checkbutton(
            circle_row1,
            text="Show red circle",
            variable=self.circle_enabled_var,
            command=self._render_current_frame,
        )
        self.circle_check.pack(side=tk.LEFT)

        self.circle_move_check = ttk.Checkbutton(
            circle_row1,
            text="Move circle",
            variable=self.circle_move_var,
        )
        self.circle_move_check.pack(side=tk.LEFT, padx=(10, 0))

        circle_row2 = ttk.Frame(controls)
        circle_row2.pack(fill=tk.X, pady=(2, 0))
        ttk.Label(circle_row2, text="Radius (px):").pack(side=tk.LEFT)
        self.circle_radius_entry = ttk.Entry(circle_row2, width=6)
        self.circle_radius_entry.pack(side=tk.LEFT, padx=(5, 0))
        self.circle_radius_entry.insert(0, "50.0")
        self.circle_radius_entry.bind("<Return>", lambda e: self._on_circle_radius_entry())
        self.circle_radius_entry.bind("<FocusOut>", lambda e: self._on_circle_radius_entry())

        self.circle_radius_scale = ttk.Scale(
            controls,
            from_=5.0,
            to=500.0,
            orient=tk.HORIZONTAL,
            command=lambda v: self._on_circle_radius_slider_changed(v),
        )
        self.circle_radius_scale.set(50.0)
        self.circle_radius_scale.pack(fill=tk.X)

        # Additional overlays associated with the red circle.
        circle_row3 = ttk.Frame(controls)
        circle_row3.pack(fill=tk.X, pady=(4, 0))

        self.circle_crosshair_check = ttk.Checkbutton(
            circle_row3,
            text="Circle center crosshair",
            variable=self.circle_crosshair_enabled_var,
            command=self._render_current_frame,
        )
        self.circle_crosshair_check.pack(anchor="w")

        circle_row4 = ttk.Frame(controls)
        circle_row4.pack(fill=tk.X, pady=(2, 0))

        self.extra_circles_check = ttk.Checkbutton(
            circle_row4,
            text="Enable extra circles",
            variable=self.extra_circles_enabled_var,
            command=self._render_current_frame,
        )
        self.extra_circles_check.pack(anchor="w")

        # Yellow-circle controls.
        ttk.Label(controls, text="Yellow circle").pack(anchor="w", pady=(8, 0))
        y_row1 = ttk.Frame(controls)
        y_row1.pack(fill=tk.X, pady=(2, 0))

        self.yellow_show_check = ttk.Checkbutton(
            y_row1,
            text="Show yellow",
            variable=self.yellow_show_var,
            command=self._render_current_frame,
        )
        self.yellow_show_check.pack(side=tk.LEFT)

        y_row2 = ttk.Frame(controls)
        y_row2.pack(fill=tk.X, pady=(2, 0))
        ttk.Label(y_row2, text="Radius (px):").pack(side=tk.LEFT)
        self.yellow_radius_entry = ttk.Entry(y_row2, width=6)
        self.yellow_radius_entry.pack(side=tk.LEFT, padx=(5, 0))
        self.yellow_radius_entry.insert(0, "20.0")
        self.yellow_radius_entry.bind("<Return>", lambda e: self._on_yellow_radius_entry())
        self.yellow_radius_entry.bind("<FocusOut>", lambda e: self._on_yellow_radius_entry())

        self.yellow_radius_scale = ttk.Scale(
            controls,
            from_=5.0,
            to=500.0,
            orient=tk.HORIZONTAL,
            command=lambda v: self._on_yellow_radius_slider_changed(v),
        )
        self.yellow_radius_scale.set(20.0)
        self.yellow_radius_scale.pack(fill=tk.X)

        # Purple-circle controls.
        ttk.Label(controls, text="Purple circle").pack(anchor="w", pady=(8, 0))
        p_row1 = ttk.Frame(controls)
        p_row1.pack(fill=tk.X, pady=(2, 0))

        self.purple_show_check = ttk.Checkbutton(
            p_row1,
            text="Show purple",
            variable=self.purple_show_var,
            command=self._render_current_frame,
        )
        self.purple_show_check.pack(side=tk.LEFT)

        p_row2 = ttk.Frame(controls)
        p_row2.pack(fill=tk.X, pady=(2, 0))
        ttk.Label(p_row2, text="Radius (px):").pack(side=tk.LEFT)
        self.purple_radius_entry = ttk.Entry(p_row2, width=6)
        self.purple_radius_entry.pack(side=tk.LEFT, padx=(5, 0))
        self.purple_radius_entry.insert(0, "30.0")
        self.purple_radius_entry.bind("<Return>", lambda e: self._on_purple_radius_entry())
        self.purple_radius_entry.bind("<FocusOut>", lambda e: self._on_purple_radius_entry())

        self.purple_radius_scale = ttk.Scale(
            controls,
            from_=5.0,
            to=500.0,
            orient=tk.HORIZONTAL,
            command=lambda v: self._on_purple_radius_slider_changed(v),
        )
        self.purple_radius_scale.set(30.0)
        self.purple_radius_scale.pack(fill=tk.X)

        # Build the scrollable live-preview area on the right.
        preview_frame = ttk.Frame(main, padding=5)
        preview_frame.pack(side=tk.RIGHT, fill=tk.BOTH, expand=True)

        preview_frame.rowconfigure(0, weight=1)
        preview_frame.columnconfigure(0, weight=1)

        # Use a gray background to show image boundaries when zoomed out.
        self.canvas = tk.Canvas(preview_frame, background="#404040")
        self.canvas.grid(row=0, column=0, sticky="nsew")

        vbar = ttk.Scrollbar(preview_frame, orient=tk.VERTICAL, command=self.canvas.yview)
        vbar.grid(row=0, column=1, sticky="ns")
        hbar = ttk.Scrollbar(preview_frame, orient=tk.HORIZONTAL, command=self.canvas.xview)
        hbar.grid(row=1, column=0, sticky="ew")

        self.canvas.configure(xscrollcommand=hbar.set, yscrollcommand=vbar.set)

        # Bind mouse input for zooming, panning, and moving the red circle.
        self.canvas.bind("<MouseWheel>", self._on_mousewheel_zoom)  # macOS and Windows.
        self.canvas.bind("<Button-4>", lambda e: self._zoom(+1, e))  # Linux: wheel up.
        self.canvas.bind("<Button-5>", lambda e: self._zoom(-1, e))  # Linux: wheel down.

        self.canvas.bind("<ButtonPress-1>", self._on_button_press)
        self.canvas.bind("<B1-Motion>", self._on_button_drag)

    # ==================== USER-INTERFACE CALLBACKS ====================

    def _on_camera_selected(self):
        idx = self.camera_combo.current()
        if idx >= 0:
            self._open_camera(idx)

    def _on_exposure_mode_changed(self):
        self._update_exposure_slider_range()
        self._apply_exposure()

    def _on_exposure_slider_changed(self, value):
        try:
            val = float(value)
        except ValueError:
            val = self.exposure_scale.get()
        self.exposure_entry.delete(0, tk.END)
        self.exposure_entry.insert(0, f"{val:.2f}")
        self._apply_exposure()

    def _on_gain_slider_changed(self, value):
        try:
            val = float(value)
        except ValueError:
            val = self.gain_scale.get()
        self.gain_entry.delete(0, tk.END)
        self.gain_entry.insert(0, f"{val:.1f}")
        self._apply_gain()

    def _on_exposure_entry(self):
        if not self.camera:
            return
        text = self.exposure_entry.get().strip()
        if not text:
            return
        try:
            value = float(text)
        except ValueError:
            return

        mode = self.exposure_mode_var.get()
        min_val, max_val = self.exposure_ranges.get(mode, (1, 1000))
        value = max(min_val, min(max_val, value))
        self.exposure_scale.set(value)
        self._apply_exposure()

    def _on_gain_entry(self):
        if not self.camera:
            return
        text = self.gain_entry.get().strip()
        if not text:
            return
        try:
            value = float(text)
        except ValueError:
            return

        value = max(self.gain_min, min(self.gain_max, value))
        self.gain_scale.set(value)
        self._apply_gain()

    def _toggle_live(self):
        if not self.camera:
            return

        if self.live_running:
            self._stop_live()
        else:
            try:
                self.camera.start_video_capture()
            except Exception as e:
                messagebox.showerror("Video error", f"Could not start video:\n{e}")
                return
            self.live_running = True
            self._update_frame()

    # Display-adjustment callbacks.

    def _parse_and_clamp(self, text, default, min_val, max_val):
        try:
            v = float(text)
        except ValueError:
            v = default
        return max(min_val, min(max_val, v))

    # Brightness callbacks.
    def _on_brightness_slider_changed(self, value):
        val = float(value)
        self.brightness = val
        self.brightness_entry.delete(0, tk.END)
        self.brightness_entry.insert(0, f"{val:.2f}")
        self._recompute_adjusted_image()
        self._render_current_frame()

    def _on_brightness_entry(self):
        val = self._parse_and_clamp(
            self.brightness_entry.get(), self.brightness, 0.1, 3.0
        )
        self.brightness = val
        self.brightness_scale.set(val)
        self.brightness_entry.delete(0, tk.END)
        self.brightness_entry.insert(0, f"{val:.2f}")
        self._recompute_adjusted_image()
        self._render_current_frame()

    # Contrast callbacks.
    def _on_contrast_slider_changed(self, value):
        val = float(value)
        self.contrast = val
        self.contrast_entry.delete(0, tk.END)
        self.contrast_entry.insert(0, f"{val:.2f}")
        self._recompute_adjusted_image()
        self._render_current_frame()

    def _on_contrast_entry(self):
        val = self._parse_and_clamp(
            self.contrast_entry.get(), self.contrast, 0.1, 3.0
        )
        self.contrast = val
        self.contrast_scale.set(val)
        self.contrast_entry.delete(0, tk.END)
        self.contrast_entry.insert(0, f"{val:.2f}")
        self._recompute_adjusted_image()
        self._render_current_frame()

    # Gamma callbacks.
    def _on_gamma_slider_changed(self, value):
        val = float(value)
        self.gamma = val
        self.gamma_entry.delete(0, tk.END)
        self.gamma_entry.insert(0, f"{val:.2f}")
        self._recompute_adjusted_image()
        self._render_current_frame()

    def _on_gamma_entry(self):
        val = self._parse_and_clamp(
            self.gamma_entry.get(), self.gamma, 0.1, 3.0
        )
        self.gamma = val
        self.gamma_scale.set(val)
        self.gamma_entry.delete(0, tk.END)
        self.gamma_entry.insert(0, f"{val:.2f}")
        self._recompute_adjusted_image()
        self._render_current_frame()

    # Saturation callbacks.
    def _on_saturation_slider_changed(self, value):
        val = float(value)
        self.saturation = val
        self.saturation_entry.delete(0, tk.END)
        self.saturation_entry.insert(0, f"{val:.2f}")
        self._recompute_adjusted_image()
        self._render_current_frame()

    def _on_saturation_entry(self):
        val = self._parse_and_clamp(
            self.saturation_entry.get(), self.saturation, 0.0, 3.0
        )
        self.saturation = val
        self.saturation_scale.set(val)
        self.saturation_entry.delete(0, tk.END)
        self.saturation_entry.insert(0, f"{val:.2f}")
        self._recompute_adjusted_image()
        self._render_current_frame()

    # Crosshair-thickness callbacks.
    def _on_crosshair_slider_changed(self, value):
        val = float(value)
        self.crosshair_thickness = max(1.0, min(10.0, val))
        self.crosshair_entry.delete(0, tk.END)
        self.crosshair_entry.insert(0, f"{self.crosshair_thickness:.1f}")
        self._render_current_frame()

    def _on_crosshair_entry(self):
        val = self._parse_and_clamp(
            self.crosshair_entry.get(), self.crosshair_thickness, 1.0, 10.0
        )
        self.crosshair_thickness = val
        self.crosshair_scale.set(val)
        self.crosshair_entry.delete(0, tk.END)
        self.crosshair_entry.insert(0, f"{val:.1f}")
        self._render_current_frame()

    # Red-circle radius callbacks.
    def _on_circle_radius_slider_changed(self, value):
        val = float(value)
        self.circle_radius_px = max(5.0, val)
        self.circle_radius_entry.delete(0, tk.END)
        self.circle_radius_entry.insert(0, f"{self.circle_radius_px:.1f}")
        self._update_extra_circle_limits()
        self._render_current_frame()

    def _on_circle_radius_entry(self):
        val = self._parse_and_clamp(
            self.circle_radius_entry.get(), self.circle_radius_px, 5.0, 99999.0
        )
        self.circle_radius_px = val
        self.circle_radius_scale.set(val)
        self.circle_radius_entry.delete(0, tk.END)
        self.circle_radius_entry.insert(0, f"{val:.1f}")
        self._update_extra_circle_limits()
        self._render_current_frame()

    # Yellow-circle radius callbacks.
    def _on_yellow_radius_slider_changed(self, value):
        val = float(value)
        max_r = max(5.0, self.circle_radius_px)
        val = max(5.0, min(max_r, val))
        self.yellow_radius_px = val
        self.yellow_radius_entry.delete(0, tk.END)
        self.yellow_radius_entry.insert(0, f"{val:.1f}")
        self._render_current_frame()

    def _on_yellow_radius_entry(self):
        max_r = max(5.0, self.circle_radius_px)
        val = self._parse_and_clamp(
            self.yellow_radius_entry.get(), self.yellow_radius_px, 5.0, max_r
        )
        self.yellow_radius_px = val
        self.yellow_radius_scale.set(val)
        self.yellow_radius_entry.delete(0, tk.END)
        self.yellow_radius_entry.insert(0, f"{val:.1f}")
        self._render_current_frame()

    # Purple-circle radius callbacks.
    def _on_purple_radius_slider_changed(self, value):
        val = float(value)
        max_r = max(5.0, self.circle_radius_px)
        val = max(5.0, min(max_r, val))
        self.purple_radius_px = val
        self.purple_radius_entry.delete(0, tk.END)
        self.purple_radius_entry.insert(0, f"{val:.1f}")
        self._render_current_frame()

    def _on_purple_radius_entry(self):
        max_r = max(5.0, self.circle_radius_px)
        val = self._parse_and_clamp(
            self.purple_radius_entry.get(), self.purple_radius_px, 5.0, max_r
        )
        self.purple_radius_px = val
        self.purple_radius_scale.set(val)
        self.purple_radius_entry.delete(0, tk.END)
        self.purple_radius_entry.insert(0, f"{val:.1f}")
        self._render_current_frame()

    def _update_extra_circle_limits(self):
        max_r = max(5.0, self.circle_radius_px)

        if not hasattr(self, "yellow_radius_scale") or not hasattr(self, "purple_radius_scale"):
            # Clamp stored values before the radius controls are available.
            self.yellow_radius_px = min(self.yellow_radius_px, max_r)
            self.purple_radius_px = min(self.purple_radius_px, max_r)
            return

        self.yellow_radius_scale.config(to=max_r)
        self.purple_radius_scale.config(to=max_r)

        # Clamp the yellow radius to the red-circle radius.
        if self.yellow_radius_px > max_r:
            self.yellow_radius_px = max_r
            self.yellow_radius_scale.set(max_r)
            self.yellow_radius_entry.delete(0, tk.END)
            self.yellow_radius_entry.insert(0, f"{max_r:.1f}")

        # Clamp the purple radius to the red-circle radius.
        if self.purple_radius_px > max_r:
            self.purple_radius_px = max_r
            self.purple_radius_scale.set(max_r)
            self.purple_radius_entry.delete(0, tk.END)
            self.purple_radius_entry.insert(0, f"{max_r:.1f}")

    # ==================== ZOOM, PAN, AND CIRCLE DRAGGING ====================

    def _on_mousewheel_zoom(self, event):
        direction = 1 if event.delta > 0 else -1
        self._zoom(direction, event)

    def _zoom(self, direction, event=None):
        if self.last_frame is None:
            return

        # Apply a small multiplicative step for smooth zooming.
        step = 1.02
        if direction > 0:
            new_zoom = self.zoom_factor * step
        else:
            new_zoom = self.zoom_factor / step

        new_zoom = max(self.min_zoom, min(self.max_zoom, new_zoom))
        if abs(new_zoom - self.zoom_factor) < 1e-3:
            return

        self.zoom_factor = new_zoom

        # Reset scrolling at minimum zoom so the full frame remains visible.
        if abs(self.zoom_factor - self.min_zoom) < 1e-3:
            self.canvas.xview_moveto(0)
            self.canvas.yview_moveto(0)

        self._render_current_frame()

    def _on_button_press(self, event):
        # Choose between moving the circle and panning from the canvas position.
        cx = self.canvas.canvasx(event.x)
        cy = self.canvas.canvasy(event.y)

        if (
            self.circle_enabled_var.get()
            and self.circle_move_var.get()
            and self._is_point_in_circle_canvas(cx, cy)
        ):
            self.drag_mode = "circle"
        else:
            self.drag_mode = "pan"
            self.canvas.scan_mark(event.x, event.y)

    def _on_button_drag(self, event):
        if self.drag_mode == "pan":
            self.canvas.scan_dragto(event.x, event.y, gain=1)
        elif self.drag_mode == "circle":
            cx = self.canvas.canvasx(event.x)
            cy = self.canvas.canvasy(event.y)
            self._move_circle_to_canvas_point(cx, cy)

    def _is_point_in_circle_canvas(self, x, y):
        if (
            self.last_frame is None
            or self.circle_center_x is None
            or self.circle_center_y is None
            or self.last_display_scale <= 0
            or not self.circle_enabled_var.get()
        ):
            return False

        cx_disp = self.last_offset_x + self.circle_center_x * self.last_display_scale
        cy_disp = self.last_offset_y + self.circle_center_y * self.last_display_scale
        r_disp = self.circle_radius_px * self.last_display_scale

        dx = x - cx_disp
        dy = y - cy_disp
        return (dx * dx + dy * dy) <= (r_disp * r_disp)

    def _move_circle_to_canvas_point(self, x, y):
        if self.last_frame is None or self.last_display_scale <= 0:
            return

        cam_h, cam_w = self.last_frame.shape[:2]

        # Convert canvas coordinates to image-pixel coordinates.
        img_x = (x - self.last_offset_x) / self.last_display_scale
        img_y = (y - self.last_offset_y) / self.last_display_scale

        img_x = max(0, min(cam_w, img_x))
        img_y = max(0, min(cam_h, img_y))

        self.circle_center_x = img_x
        self.circle_center_y = img_y
        self._render_current_frame()

    # ==================== CAMERA SETTINGS ====================

    def _update_exposure_slider_range(self):
        mode = self.exposure_mode_var.get()
        min_val, max_val = self.exposure_ranges.get(mode, (1, 1000))
        self.exposure_scale.config(from_=min_val, to=max_val)

    def _apply_gain(self):
        if not self.camera:
            return
        gain_val = float(self.gain_scale.get())
        self.gain_value_label.config(text=f"{gain_val:.0f}")
        try:
            self.camera.set_control_value(asi.ASI_GAIN, int(gain_val))
        except Exception as e:
            print("Failed to set gain:", e, file=sys.stderr)

    def _apply_exposure(self):
        if not self.camera:
            return

        mode = self.exposure_mode_var.get()
        factor = self.exposure_modes.get(mode, 1000)
        raw_val = float(self.exposure_scale.get())
        exposure_us = int(raw_val * factor)

        exposure_us = max(self.exposure_min_us, min(exposure_us, self.exposure_max_us))

        if mode == "µs":
            label_text = f"{raw_val:.0f} µs"
        elif mode == "ms":
            label_text = f"{raw_val:.0f} ms"
        else:
            label_text = f"{raw_val:.1f} s"
        self.exposure_value_label.config(text=label_text)

        try:
            self.camera.set_control_value(asi.ASI_EXPOSURE, exposure_us)
        except Exception as e:
            print("Failed to set exposure:", e, file=sys.stderr)

    # ==================== LIVE VIEW AND FILE SAVING ====================

    def _stop_live(self):
        self.live_running = False
        if self.camera:
            try:
                self.camera.stop_video_capture()
            except Exception:
                pass
        self.live_button.config(text="Start Live")
        # Reset the frame-rate display.
        self.last_frame_time = None
        self.current_fps = 0.0
        self.fps_label.config(text="FPS: 0.0")

    def _update_frame(self):
        if not self.live_running or not self.camera:
            return

        try:
            frame = self.camera.capture_video_frame(timeout=2000)
        except Exception as e:
            print("Error capturing frame:", e, file=sys.stderr)
            self.root.after(100, self._update_frame)
            return

        # Update the smoothed frame-rate estimate.
        now = time.time()
        if self.last_frame_time is not None:
            dt = now - self.last_frame_time
            if dt > 0:
                inst_fps = 1.0 / dt
                if self.current_fps > 0:
                    self.current_fps = 0.8 * self.current_fps + 0.2 * inst_fps
                else:
                    self.current_fps = inst_fps
                self.fps_label.config(text=f"FPS: {self.current_fps:.1f}")
        self.last_frame_time = now

        # Read the temperature in the SDK's 0.1-degree-Celsius units.
        try:
            temp_raw, _ = self.camera.get_control_value(asi.ASI_TEMPERATURE)
            temp_c = temp_raw / 10.0
            self.temp_label.config(text=f"Temp: {temp_c:.1f} °C")
        except Exception:
            pass

        self.last_frame = frame

        # Recompute the adjusted display image once for each captured frame.
        self._recompute_adjusted_image()
        self._render_current_frame()

        self.live_button.config(text="Stop Live")
        self.root.after(self.frame_interval_ms, self._update_frame)

    # ==================== IMAGE PROCESSING AND RENDERING ====================

    def _apply_image_adjustments(self, frame: np.ndarray) -> np.ndarray:
        """Apply display adjustments to an 8-bit RGB NumPy array."""
        img = frame.astype(np.float32) / 255.0

        # Scale all channels to adjust brightness.
        img *= self.brightness

        # Adjust contrast around normalized mid-gray (0.5).
        img = (img - 0.5) * self.contrast + 0.5
        img = np.clip(img, 0.0, 1.0)

        # Apply gamma correction when the setting is non-neutral.
        if abs(self.gamma - 1.0) > 1e-3:
            g = max(self.gamma, 0.01)
            img = img ** (1.0 / g)
            img = np.clip(img, 0.0, 1.0)

        # Interpolate relative to luminance to adjust saturation.
        if abs(self.saturation - 1.0) > 1e-3:
            lum = img[..., 0] * 0.299 + img[..., 1] * 0.587 + img[..., 2] * 0.114
            lum = lum[..., np.newaxis]
            img = lum + (img - lum) * self.saturation
            img = np.clip(img, 0.0, 1.0)

        return (img * 255).astype(np.uint8)

    def _recompute_adjusted_image(self):
        """Update the cached display image from the latest frame and settings."""
        if self.last_frame is None:
            self.adjusted_pil = None
            return

        frame = self.last_frame

        # Avoid numerical processing when every display adjustment is neutral.
        if (
            abs(self.brightness - 1.0) < 1e-3
            and abs(self.contrast - 1.0) < 1e-3
            and abs(self.gamma - 1.0) < 1e-3
            and abs(self.saturation - 1.0) < 1e-3
        ):
            self.adjusted_pil = Image.fromarray(frame)
            return

        try:
            adj = self._apply_image_adjustments(frame)
        except Exception as e:
            print("Adjustment error:", e, file=sys.stderr)
            adj = frame

        self.adjusted_pil = Image.fromarray(adj)

    def _render_current_frame(self):
        """Render the cached image at the current zoom with centered overlays."""
        if self.last_frame is None or self.adjusted_pil is None:
            return

        cam_h, cam_w = self.last_frame.shape[:2]

        # Obtain the canvas dimensions used for fit-to-window scaling.
        canvas_w = self.canvas.winfo_width() or cam_w
        canvas_h = self.canvas.winfo_height() or cam_h
        canvas_size = (canvas_w, canvas_h)

        # Fit the image inside the canvas while preserving its aspect ratio.
        if self.base_scale is None or canvas_size != self.last_canvas_size:
            self.base_scale = min(canvas_w / cam_w, canvas_h / cam_h)
            self.last_canvas_size = canvas_size

        display_scale = self.base_scale * self.zoom_factor
        new_w = max(1, int(cam_w * display_scale))
        new_h = max(1, int(cam_h * display_scale))

        img_resized = self.adjusted_pil.resize((new_w, new_h), Image.LANCZOS)

        self.tk_image = ImageTk.PhotoImage(img_resized)
        self.canvas.delete("all")

        # Center the image along dimensions where it is smaller than the canvas.
        offset_x = max(0, (canvas_w - new_w) // 2)
        offset_y = max(0, (canvas_h - new_h) // 2)

        # Cache the display transform for overlay placement and circle dragging.
        self.last_offset_x = offset_x
        self.last_offset_y = offset_y
        self.last_display_scale = display_scale

        # Draw the resized image.
        self.canvas.create_image(offset_x, offset_y, image=self.tk_image, anchor="nw")

        thickness = max(1.0, float(self.crosshair_thickness))

        # Draw the main crosshair across the full displayed image.
        if self.crosshair_enabled_var.get():
            cx_img = offset_x + new_w / 2.0
            cy_img = offset_y + new_h / 2.0

            self.canvas.create_line(
                offset_x, cy_img, offset_x + new_w, cy_img,
                fill="cyan",
                width=thickness
            )
            self.canvas.create_line(
                cx_img, offset_y, cx_img, offset_y + new_h,
                fill="cyan",
                width=thickness
            )

        # Draw the red circle and its related overlays.
        if (
            self.circle_center_x is not None
            and self.circle_center_y is not None
            and self.circle_radius_px > 0
        ):
            cx_disp = offset_x + self.circle_center_x * display_scale
            cy_disp = offset_y + self.circle_center_y * display_scale
            r_disp = self.circle_radius_px * display_scale

            if self.circle_enabled_var.get():
                # Draw the primary red circle.
                self.canvas.create_oval(
                    cx_disp - r_disp,
                    cy_disp - r_disp,
                    cx_disp + r_disp,
                    cy_disp + r_disp,
                    outline="red",
                    width=thickness,
                )

            # Draw a red crosshair spanning the circle's diameter.
            if self.circle_crosshair_enabled_var.get():
                # In display coordinates, the diameter is twice the radius.
                self.canvas.create_line(
                    cx_disp - r_disp, cy_disp,
                    cx_disp + r_disp, cy_disp,
                    fill="red",
                    width=thickness
                )
                self.canvas.create_line(
                    cx_disp, cy_disp - r_disp,
                    cx_disp, cy_disp + r_disp,
                    fill="red",
                    width=thickness
                )

            # Draw enabled auxiliary circles around the same center.
            if self.extra_circles_enabled_var.get():
                # Draw the yellow auxiliary circle.
                if self.yellow_show_var.get():
                    r_y_disp = self.yellow_radius_px * display_scale
                    self.canvas.create_oval(
                        cx_disp - r_y_disp,
                        cy_disp - r_y_disp,
                        cx_disp + r_y_disp,
                        cy_disp + r_y_disp,
                        outline="yellow",
                        width=thickness,
                    )
                # Draw the purple auxiliary circle in magenta.
                if self.purple_show_var.get():
                    r_p_disp = self.purple_radius_px * display_scale
                    self.canvas.create_oval(
                        cx_disp - r_p_disp,
                        cy_disp - r_p_disp,
                        cx_disp + r_p_disp,
                        cy_disp + r_p_disp,
                        outline="magenta",
                        width=thickness,
                    )

        # Ensure the scrollable region contains both the canvas and image bounds.
        max_w = max(canvas_w, offset_x + new_w)
        max_h = max(canvas_h, offset_y + new_h)
        self.canvas.config(scrollregion=(0, 0, max_w, max_h))

    def _save_frame(self):
        if self.last_frame is None:
            messagebox.showinfo("No frame", "No frame captured yet.")
            return

        filetypes = [
            ("PNG", "*.png"),
            ("JPEG", "*.jpg;*.jpeg"),
            ("TIFF", "*.tif;*.tiff"),
            ("All files", "*.*"),
        ]
        filename = filedialog.asksaveasfilename(
            defaultextension=".png", filetypes=filetypes
        )
        if not filename:
            return

        try:
            img = Image.fromarray(self.last_frame)
            img.save(filename)
            messagebox.showinfo("Saved", f"Saved image to:\n{filename}")
        except Exception as e:
            messagebox.showerror("Save error", f"Could not save image:\n{e}")


def main():
    root = tk.Tk()
    app = ASIControlApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
