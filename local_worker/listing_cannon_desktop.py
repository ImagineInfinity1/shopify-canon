from __future__ import annotations

import json
import os
import queue
import shutil
import sys
import threading
import time
import tkinter as tk
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

import requests
from PIL import Image, ImageDraw

try:
    from tkinterdnd2 import DND_FILES, TkinterDnD
except ImportError:
    DND_FILES = None
    TkinterDnD = None

from listing_cannon_local_worker import (
    IMAGE_EXTENSIONS,
    PSD_EXTENSIONS,
    config,
    default_app_data_root,
    ensure_dirs,
    load_env_file,
    prepare_ready_group,
    publish_stage,
    render_stage,
    sorted_frames,
    unique_path,
)


CODE_ROOT = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
DATA_ROOT = default_app_data_root()
FRAME_ORDER_FILE = DATA_ROOT / "frame_order.json"
QUEUE_FILE = DATA_ROOT / "artwork_queue.json"
WATCH_FILE = DATA_ROOT / "watch_folder.json"
SETTINGS_FILE = DATA_ROOT / "settings.json"
BUNDLED_ICON_FILE = CODE_ROOT / "listing_cannon.ico"
USER_ICON_FILE = DATA_ROOT / "listing_cannon.ico"
DEFAULT_SETTINGS = {
    "LISTING_CANNON_URL": "https://listing-cannon.onrender.com",
    "LOCAL_WORKER_TOKEN": "",
    "PROFILE_NAME": "MAIN 1",
    "REVIEW_BEFORE_PUBLISH": "false",
    "WAIT_FOR_COMPLETION": "true",
    "PRE_FRAMED_MODE": "false",
    "IMAGES_PER_LISTING": "4",
    "ANALYSIS_IMAGE_INDEX": "1",
}
BG = "#08111f"
PANEL = "#0f1b2d"
PANEL_2 = "#17243a"
TEXT = "#eef4ff"
MUTED = "#9fb0c7"
BLUE = "#2563eb"
GREEN = "#22c55e"
ORANGE = "#f59e0b"
RED = "#ef4444"
RED_DARK = "#3b0710"
RED_LINE = "#ef4444"
QUEUE_STATUSES = {"pending", "processing", "done", "failed"}
BATCH_DELAY_SECONDS = 0.25
WATCH_SCAN_SECONDS = 60
WATCH_MIN_AGE_SECONDS = 15


def create_icon():
    if BUNDLED_ICON_FILE.exists():
        return BUNDLED_ICON_FILE
    DATA_ROOT.mkdir(parents=True, exist_ok=True)
    if USER_ICON_FILE.exists():
        return USER_ICON_FILE
    img = Image.new("RGBA", (256, 256), (15, 23, 42, 255))
    draw = ImageDraw.Draw(img)
    draw.rounded_rectangle((34, 34, 222, 222), radius=42, fill=(124, 58, 237, 255))
    draw.rounded_rectangle((66, 58, 180, 198), radius=12, outline=(255, 255, 255, 255), width=10)
    draw.polygon([(154, 82), (206, 52), (174, 112)], fill=(34, 197, 94, 255))
    draw.line((82, 158, 160, 92), fill=(255, 255, 255, 255), width=12)
    img.save(USER_ICON_FILE, format="ICO", sizes=[(256, 256), (64, 64), (32, 32), (16, 16)])
    return USER_ICON_FILE


def seed_user_data(source_root: Path, data_root: Path) -> None:
    data_root.mkdir(parents=True, exist_ok=True)
    for filename in ("frame_order.json",):
        source = source_root / filename
        dest = data_root / filename
        if source.exists() and not dest.exists():
            shutil.copy2(source, dest)

    source_frames = source_root / "frames"
    dest_frames = data_root / "frames"
    if not source_frames.exists():
        return
    dest_frames.mkdir(parents=True, exist_ok=True)
    for source in source_frames.iterdir():
        dest = dest_frames / source.name
        if source.is_file() and source.suffix.lower() in PSD_EXTENSIONS and not dest.exists():
            shutil.copy2(source, dest)


def seed_known_user_data(data_root: Path) -> None:
    candidates = [
        Path(__file__).resolve().parent,
        Path.home() / "listing-cannon-inspect" / "local_worker",
    ]
    seen: set[Path] = set()
    for source_root in candidates:
        resolved = source_root.resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        seed_user_data(resolved, data_root)


def load_app_settings() -> dict[str, str]:
    settings = DEFAULT_SETTINGS.copy()
    if SETTINGS_FILE.exists():
        try:
            data = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                for key in settings:
                    value = data.get(key)
                    if value is not None:
                        settings[key] = str(value).strip()
        except Exception:
            pass
    else:
        for key in settings:
            value = os.environ.get(key)
            if value:
                settings[key] = value.strip()
        save_app_settings(settings)
    return settings


def save_app_settings(settings: dict[str, str]) -> None:
    DATA_ROOT.mkdir(parents=True, exist_ok=True)
    payload = {
        key: str(settings.get(key, DEFAULT_SETTINGS[key])).strip()
        for key in DEFAULT_SETTINGS
    }
    SETTINGS_FILE.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def apply_app_settings(settings: dict[str, str]) -> None:
    for key, default in DEFAULT_SETTINGS.items():
        os.environ[key] = str(settings.get(key, default)).strip()


class SettingsDialog:
    def __init__(self, app: "ListingCannonDesktop", first_run: bool = False):
        self.app = app
        self.first_run = first_run
        self.window = tk.Toplevel(app.root)
        self.window.title("Listing Cannon Settings")
        self.window.configure(bg=BG)
        self.window.resizable(False, False)
        self.window.transient(app.root)
        self.window.grab_set()

        self.vars = {
            "LISTING_CANNON_URL": tk.StringVar(value=app.settings.get("LISTING_CANNON_URL", DEFAULT_SETTINGS["LISTING_CANNON_URL"])),
            "LOCAL_WORKER_TOKEN": tk.StringVar(value=app.settings.get("LOCAL_WORKER_TOKEN", "")),
            "PROFILE_NAME": tk.StringVar(value=app.settings.get("PROFILE_NAME", DEFAULT_SETTINGS["PROFILE_NAME"])),
            "REVIEW_BEFORE_PUBLISH": tk.BooleanVar(value=app.settings.get("REVIEW_BEFORE_PUBLISH", "false").lower() in {"1", "true", "yes"}),
            "WAIT_FOR_COMPLETION": tk.BooleanVar(value=app.settings.get("WAIT_FOR_COMPLETION", "true").lower() in {"1", "true", "yes"}),
        }
        self._build()
        self.window.protocol("WM_DELETE_WINDOW", self.cancel)
        self.window.update_idletasks()
        x = app.root.winfo_x() + (app.root.winfo_width() // 2) - (self.window.winfo_width() // 2)
        y = app.root.winfo_y() + (app.root.winfo_height() // 2) - (self.window.winfo_height() // 2)
        self.window.geometry(f"+{max(0, x)}+{max(0, y)}")

    def _build(self):
        frame = tk.Frame(self.window, bg=BG, padx=22, pady=18)
        frame.grid(row=0, column=0, sticky="nsew")
        frame.grid_columnconfigure(1, weight=1)

        title = "Connect to Listing Cannon" if self.first_run else "Settings"
        tk.Label(frame, text=title, bg=BG, fg=TEXT, font=("Segoe UI", 16, "bold")).grid(row=0, column=0, columnspan=2, sticky="w")
        tk.Label(
            frame,
            text="Enter the Render connection details once. They are saved on this PC.",
            bg=BG,
            fg=MUTED,
            font=("Segoe UI", 10),
        ).grid(row=1, column=0, columnspan=2, sticky="w", pady=(3, 16))

        self._row(frame, 2, "Render URL", "LISTING_CANNON_URL")
        self._row(frame, 3, "Worker Token", "LOCAL_WORKER_TOKEN", show="*")
        self._row(frame, 4, "Default Profile", "PROFILE_NAME")

        tk.Checkbutton(
            frame,
            text="Send listings for manual review before publish",
            variable=self.vars["REVIEW_BEFORE_PUBLISH"],
            bg=BG,
            fg=TEXT,
            selectcolor=PANEL,
            activebackground=BG,
            activeforeground=TEXT,
        ).grid(row=5, column=0, columnspan=2, sticky="w", pady=(10, 0))
        tk.Checkbutton(
            frame,
            text="Wait for Listing Cannon to finish before marking artwork completed",
            variable=self.vars["WAIT_FOR_COMPLETION"],
            bg=BG,
            fg=TEXT,
            selectcolor=PANEL,
            activebackground=BG,
            activeforeground=TEXT,
        ).grid(row=6, column=0, columnspan=2, sticky="w")

        buttons = tk.Frame(frame, bg=BG)
        buttons.grid(row=7, column=0, columnspan=2, sticky="e", pady=(18, 0))
        ttk.Button(buttons, text="Cancel", command=self.cancel, style="Dark.TButton").grid(row=0, column=0, padx=(0, 8))
        ttk.Button(buttons, text="Save", command=self.save, style="Accent.TButton").grid(row=0, column=1)

    def _row(self, parent, row: int, label: str, key: str, show: str | None = None):
        tk.Label(parent, text=label, bg=BG, fg=TEXT, font=("Segoe UI", 10, "bold")).grid(row=row, column=0, sticky="w", pady=6, padx=(0, 12))
        entry = tk.Entry(parent, textvariable=self.vars[key], show=show or "", bg="#0b1220", fg=TEXT, insertbackground=TEXT, relief="flat", width=54)
        entry.grid(row=row, column=1, sticky="ew", pady=6)

    def save(self):
        url = self.vars["LISTING_CANNON_URL"].get().strip().rstrip("/")
        token = self.vars["LOCAL_WORKER_TOKEN"].get().strip()
        profile = self.vars["PROFILE_NAME"].get().strip() or DEFAULT_SETTINGS["PROFILE_NAME"]
        if not url.startswith(("http://", "https://")):
            messagebox.showerror("Invalid Render URL", "Enter a full Render URL starting with https://.", parent=self.window)
            return
        if not token:
            messagebox.showerror("Missing Worker Token", "Enter the worker token from the Render environment settings.", parent=self.window)
            return
        # Merge on top of existing settings so keys this dialog does not edit
        # (e.g. pre-framed mode prefs) are not reset to defaults on save.
        settings = {
            **self.app.settings,
            "LISTING_CANNON_URL": url,
            "LOCAL_WORKER_TOKEN": token,
            "PROFILE_NAME": profile,
            "REVIEW_BEFORE_PUBLISH": "true" if self.vars["REVIEW_BEFORE_PUBLISH"].get() else "false",
            "WAIT_FOR_COMPLETION": "true" if self.vars["WAIT_FOR_COMPLETION"].get() else "false",
        }
        self.app.apply_settings(settings)
        self.window.destroy()
        self.app.refresh_connection_async()

    def cancel(self):
        self.window.destroy()


class ListingCannonDesktop:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("Listing Cannon PSD Framer")
        self.root.geometry("1120x720")
        self.root.minsize(860, 560)

        seed_known_user_data(DATA_ROOT)
        load_env_file(DATA_ROOT / ".env")
        self.settings = load_app_settings()
        apply_app_settings(self.settings)
        icon_file = create_icon()
        try:
            self.root.iconbitmap(str(icon_file))
        except tk.TclError:
            pass

        self.dirs = ensure_dirs(DATA_ROOT)
        self.queue_items: list[dict] = self.load_queue()
        self.watch_folder: Path | None = self.load_watch_folder()
        self.frame_paths: list[Path] = []
        self.processing = False
        self.pause_requested = False
        self.batch_started_at: float | None = None
        self.batch_listed_count = 0
        self.connected = False
        self.connection_check_in_flight = False
        self.connection_failure_count = 0
        self.profiles: list[str] = []
        self.log_queue: queue.Queue[str] = queue.Queue()
        self.drag_start_index: int | None = None

        self._build_style()
        self._build_ui()
        self.on_pre_framed_changed()
        self.load_frames()
        self.render_queue()
        if self.settings.get("LOCAL_WORKER_TOKEN"):
            self.refresh_connection_async()
        else:
            self._set_connection_status("red", "Setup needed", "Open Settings")
            self.root.after(250, lambda: self.open_settings(first_run=True))
        self._drain_logs()
        self.root.after(60000, self._connection_loop)
        self.root.after(5000, self._watch_loop)

    def _build_style(self):
        style = ttk.Style()
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure(".", background=BG, foreground=TEXT, font=("Segoe UI", 9))
        style.configure("Panel.TFrame", background=PANEL)
        style.configure("Panel2.TFrame", background=PANEL_2)
        style.configure("Title.TLabel", background=BG, foreground=TEXT, font=("Segoe UI", 18, "bold"))
        style.configure("Subtle.TLabel", background=BG, foreground=MUTED, font=("Segoe UI", 9))
        style.configure("PanelTitle.TLabel", background=PANEL, foreground=TEXT, font=("Segoe UI", 11, "bold"))
        style.configure("PanelText.TLabel", background=PANEL, foreground=MUTED, font=("Segoe UI", 9))
        style.configure("Accent.TButton", background=BLUE, foreground="#ffffff", font=("Segoe UI", 10, "bold"), padding=8)
        style.map("Accent.TButton", background=[("active", "#1d4ed8"), ("disabled", "#475569")])
        style.configure("Dark.TButton", background="#1f2f46", foreground=TEXT, padding=6)
        style.map("Dark.TButton", background=[("active", "#334155")])
        style.configure("TCombobox", fieldbackground="#0b1220", background="#0b1220", foreground=TEXT)
        self.root.configure(bg=BG)

    def _build_ui(self):
        outer = tk.Frame(self.root, bg=BG, padx=14, pady=12)
        outer.pack(fill="both", expand=True)
        outer.grid_columnconfigure(0, weight=38, uniform="main")
        outer.grid_columnconfigure(1, weight=62, uniform="main")
        outer.grid_rowconfigure(2, weight=1)

        header = tk.Frame(outer, bg=BG)
        header.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 8))
        header.grid_columnconfigure(0, weight=1)
        ttk.Label(header, text="Listing Cannon PSD Framer", style="Title.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Label(
            header,
            text="Frame raw artwork locally, then send finished mockups into Listing Cannon.",
            style="Subtle.TLabel",
        ).grid(row=1, column=0, sticky="w", pady=(2, 0))

        status_bar = tk.Frame(header, bg=BG)
        status_bar.grid(row=0, column=1, rowspan=2, sticky="e")
        self.status_dot = tk.Canvas(status_bar, width=16, height=16, bg=BG, highlightthickness=0)
        self.status_dot.grid(row=0, column=0, padx=(0, 8))
        self.status_label = tk.Label(status_bar, text="Connecting", bg=BG, fg=ORANGE, font=("Segoe UI", 11, "bold"))
        self.status_label.grid(row=0, column=1, sticky="w")
        self.status_detail = tk.Label(status_bar, text="", bg=BG, fg=MUTED, font=("Segoe UI", 9))
        self.status_detail.grid(row=1, column=0, columnspan=2, sticky="e")
        ttk.Button(status_bar, text="Settings", command=self.open_settings, style="Dark.TButton").grid(row=2, column=0, columnspan=2, sticky="ew", pady=(5, 0))
        self._set_connection_status("orange", "Connecting", "Checking Render")

        self.warning_label = tk.Label(
            outer,
            text=(
                "CHECK BEFORE SENDING: SKU pattern, size/price variants, product category, "
                "and collections are assigned properly."
            ),
            bg=RED_DARK,
            fg="#fecaca",
            anchor="w",
            justify="left",
            padx=12,
            pady=7,
            font=("Segoe UI", 10, "bold"),
            highlightthickness=1,
            highlightbackground=RED_LINE,
        )
        self.warning_label.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(0, 10))

        left = tk.Frame(outer, bg=PANEL, padx=12, pady=12)
        left.grid(row=2, column=0, sticky="nsew", padx=(0, 10))
        left.grid_rowconfigure(4, weight=1)
        left.grid_columnconfigure(0, weight=1)

        right = tk.Frame(outer, bg=PANEL, padx=12, pady=12)
        right.grid(row=2, column=1, sticky="nsew")
        right.grid_columnconfigure(0, weight=1)
        right.grid_rowconfigure(3, weight=1)
        right.grid_rowconfigure(7, weight=1)
        right.bind("<Configure>", self._resize_notes)

        ttk.Label(left, text="Saved Profile", style="PanelTitle.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Label(left, text="This is the Listing Cannon profile used for AI prompts and settings.", style="PanelText.TLabel").grid(row=1, column=0, sticky="w", pady=(3, 8))
        self.profile_var = tk.StringVar(value=config("PROFILE_NAME", "MAIN 1") or "MAIN 1")
        profile_row = tk.Frame(left, bg=PANEL)
        profile_row.grid(row=2, column=0, sticky="ew", pady=(0, 12))
        profile_row.grid_columnconfigure(0, weight=1)
        self.profile_combo = ttk.Combobox(profile_row, textvariable=self.profile_var, values=[self.profile_var.get()], state="readonly")
        self.profile_combo.grid(row=0, column=0, sticky="ew", padx=(0, 8))
        ttk.Button(profile_row, text="Refresh", command=self.refresh_connection_async, style="Dark.TButton").grid(row=0, column=1)
        self.profile_combo.bind("<<ComboboxSelected>>", self.on_profile_selected)

        ttk.Label(left, text="PSD Mockups", style="PanelTitle.TLabel").grid(row=3, column=0, sticky="w")
        self.frame_drop = tk.Label(
            left,
            text="Drop PSD/PSB mockups here",
            bg="#172554",
            fg="#dbeafe",
            relief="flat",
            height=3,
            font=("Segoe UI", 10, "bold"),
        )
        self.frame_drop.grid(row=4, column=0, sticky="nsew", pady=(8, 8))
        self.frame_drop.bind("<Button-1>", lambda _event: self.add_frames())
        if DND_FILES:
            self.frame_drop.drop_target_register(DND_FILES)
            self.frame_drop.dnd_bind("<<Drop>>", self.on_frame_drop)

        self.frames_list = tk.Listbox(left, height=8, bg="#07101f", fg=TEXT, selectbackground=BLUE, relief="flat", activestyle="none", font=("Segoe UI", 9))
        self.frames_list.grid(row=5, column=0, sticky="nsew")
        self.frames_list.bind("<Button-1>", self._frame_drag_start)
        self.frames_list.bind("<ButtonRelease-1>", self._frame_click_release)
        self.frames_list.bind("<B1-Motion>", self._frame_drag_motion)

        frame_buttons = tk.Frame(left, bg=PANEL)
        frame_buttons.grid(row=6, column=0, sticky="ew", pady=(8, 0))
        for idx in range(5):
            frame_buttons.grid_columnconfigure(idx, weight=1)
        ttk.Button(frame_buttons, text="+ PSDs", command=self.add_frames, style="Dark.TButton").grid(row=0, column=0, sticky="ew", padx=(0, 5))
        ttk.Button(frame_buttons, text="↑", command=lambda: self.move_frame(-1), style="Dark.TButton").grid(row=0, column=1, sticky="ew", padx=5)
        ttk.Button(frame_buttons, text="↓", command=lambda: self.move_frame(1), style="Dark.TButton").grid(row=0, column=2, sticky="ew", padx=5)
        ttk.Button(frame_buttons, text="X", command=self.delete_selected_frame, style="Dark.TButton").grid(row=0, column=3, sticky="ew", padx=5)
        ttk.Button(frame_buttons, text="Folder", command=self.open_frames_folder, style="Dark.TButton").grid(row=0, column=4, sticky="ew", padx=(5, 0))

        ttk.Label(right, text="Raw Artwork Batch", style="PanelTitle.TLabel").grid(row=0, column=0, sticky="w")
        self.image_drop = tk.Label(
            right,
            text="Drop raw artwork images here",
            bg="#12312a",
            fg="#bbf7d0",
            height=4,
            relief="flat",
            font=("Segoe UI", 14, "bold"),
        )
        self.image_drop.grid(row=1, column=0, sticky="ew", pady=(8, 8))
        self.image_drop.bind("<Button-1>", lambda _event: self.add_images())
        if DND_FILES:
            self.image_drop.drop_target_register(DND_FILES)
            self.image_drop.dnd_bind("<<Drop>>", self.on_image_drop)

        preframed_bar = tk.Frame(right, bg=PANEL_2, padx=10, pady=7)
        preframed_bar.grid(row=2, column=0, sticky="ew", pady=(0, 8))
        self.pre_framed_var = tk.BooleanVar(
            value=self.settings.get("PRE_FRAMED_MODE", "false").lower() in {"1", "true", "yes"}
        )
        tk.Checkbutton(
            preframed_bar,
            text="Pre-framed mode (images are finished mockups — skip PSD framing)",
            variable=self.pre_framed_var,
            command=self.on_pre_framed_changed,
            bg=PANEL_2,
            fg=TEXT,
            selectcolor=PANEL,
            activebackground=PANEL_2,
            activeforeground=TEXT,
            font=("Segoe UI", 9, "bold"),
        ).grid(row=0, column=0, sticky="w")
        options = tk.Frame(preframed_bar, bg=PANEL_2)
        options.grid(row=1, column=0, sticky="w", pady=(4, 0))
        tk.Label(options, text="Images per listing:", bg=PANEL_2, fg=MUTED, font=("Segoe UI", 9)).grid(row=0, column=0, padx=(18, 6))
        self.images_per_listing_var = tk.StringVar(value=self.settings.get("IMAGES_PER_LISTING", "4") or "4")
        self.images_per_listing_spin = tk.Spinbox(
            options, from_=1, to=20, width=4, textvariable=self.images_per_listing_var,
            command=self.on_pre_framed_changed, bg="#0b1220", fg=TEXT, insertbackground=TEXT, relief="flat",
        )
        self.images_per_listing_spin.grid(row=0, column=1)
        tk.Label(options, text="Analyze image #:", bg=PANEL_2, fg=MUTED, font=("Segoe UI", 9)).grid(row=0, column=2, padx=(16, 6))
        self.analysis_index_var = tk.StringVar(value=self.settings.get("ANALYSIS_IMAGE_INDEX", "1") or "1")
        self.analysis_index_spin = tk.Spinbox(
            options, from_=1, to=20, width=4, textvariable=self.analysis_index_var,
            command=self.on_pre_framed_changed, bg="#0b1220", fg=TEXT, insertbackground=TEXT, relief="flat",
        )
        self.analysis_index_spin.grid(row=0, column=3)
        self.images_per_listing_spin.bind("<FocusOut>", lambda _e: self.on_pre_framed_changed())
        self.analysis_index_spin.bind("<FocusOut>", lambda _e: self.on_pre_framed_changed())

        self.image_list = tk.Listbox(right, height=7, bg="#07101f", fg=TEXT, selectbackground=BLUE, relief="flat", activestyle="none", font=("Segoe UI", 9))
        self.image_list.grid(row=3, column=0, sticky="nsew")

        controls = tk.Frame(right, bg=PANEL)
        controls.grid(row=4, column=0, sticky="ew", pady=8)
        controls.grid_columnconfigure((0, 1, 2, 3, 4, 5), weight=1)
        ttk.Button(controls, text="Add Images", command=self.add_images, style="Dark.TButton").grid(row=0, column=0, sticky="ew", padx=(0, 4))
        ttk.Button(controls, text="Add Folder", command=self.add_image_folder, style="Dark.TButton").grid(row=0, column=1, sticky="ew", padx=4)
        ttk.Button(controls, text="Retry Failed", command=self.retry_failed, style="Dark.TButton").grid(row=0, column=2, sticky="ew", padx=4)
        ttk.Button(controls, text="Clear Done", command=self.clear_done, style="Dark.TButton").grid(row=0, column=3, sticky="ew", padx=4)
        ttk.Button(controls, text="Clear Queue", command=self.clear_images, style="Dark.TButton").grid(row=0, column=4, sticky="ew", padx=4)
        self.start_button = ttk.Button(controls, text="Frame and Send", command=self.start_processing, style="Accent.TButton")
        self.start_button.grid(row=0, column=5, sticky="ew", padx=(4, 0))
        self.start_button.configure(state="disabled")
        self.watch_button = ttk.Button(controls, text="Watch Folder", command=self.choose_watch_folder, style="Dark.TButton")
        self.watch_button.grid(row=1, column=0, columnspan=3, sticky="ew", pady=(7, 0), padx=(0, 4))
        self.pause_button = ttk.Button(controls, text="Pause", command=self.pause_processing, style="Dark.TButton")
        self.pause_button.grid(row=1, column=3, columnspan=3, sticky="ew", pady=(7, 0), padx=(4, 0))
        self.pause_button.configure(state="disabled")

        self.work_status = tk.Label(right, text="Waiting for Render connection", bg=PANEL, fg=MUTED, anchor="w", font=("Segoe UI", 10))
        self.work_status.grid(row=5, column=0, sticky="ew", pady=(0, 2))

        self.batch_stats = tk.Label(right, text="", bg=PANEL, fg=GREEN, anchor="w", font=("Segoe UI", 10, "bold"))
        self.batch_stats.grid(row=6, column=0, sticky="ew", pady=(0, 6))

        self.log_text = tk.Text(right, height=9, wrap="word", bg="#030712", fg="#d1d5db", insertbackground=TEXT, relief="flat", font=("Consolas", 8))
        self.log_text.grid(row=7, column=0, sticky="nsew")

    def _resize_notes(self, event):
        if hasattr(self, "warning_label"):
            self.warning_label.configure(wraplength=max(420, event.width + 320))

    def _set_connection_status(self, status: str, label: str, detail: str = ""):
        color = {"green": GREEN, "orange": ORANGE, "red": RED}.get(status, ORANGE)
        self.status_dot.delete("all")
        self.status_dot.create_oval(2, 2, 14, 14, fill=color, outline=color)
        self.status_label.configure(text=label, fg=color)
        self.status_detail.configure(text=detail)
        self.connected = status == "green"
        self.update_queue_status()
        if hasattr(self, "work_status") and not self.processing:
            if self.connected:
                self.update_queue_status()
            else:
                self.work_status.configure(text="Waiting for Render connection")

    def apply_settings(self, settings: dict[str, str]):
        self.settings = settings
        save_app_settings(settings)
        apply_app_settings(settings)
        if hasattr(self, "profile_var"):
            self.profile_var.set(settings.get("PROFILE_NAME", DEFAULT_SETTINGS["PROFILE_NAME"]) or DEFAULT_SETTINGS["PROFILE_NAME"])
            self.profile_combo.configure(values=[self.profile_var.get()])
        self.log("Settings saved.")

    def open_settings(self, first_run: bool = False):
        SettingsDialog(self, first_run=first_run)

    def load_queue(self) -> list[dict]:
        if not QUEUE_FILE.exists():
            return []
        try:
            data = json.loads(QUEUE_FILE.read_text(encoding="utf-8"))
        except Exception:
            return []
        if not isinstance(data, list):
            return []
        items = []
        for raw_item in data:
            if not isinstance(raw_item, dict):
                continue
            source = str(raw_item.get("source_path") or "").strip()
            if not source:
                continue
            status = str(raw_item.get("status") or "pending").strip().lower()
            if status not in QUEUE_STATUSES:
                status = "pending"
            if status == "processing":
                status = "pending"
            try:
                added_at = float(raw_item.get("added_at") or time.time())
            except (TypeError, ValueError):
                added_at = time.time()
            try:
                updated_at = float(raw_item.get("updated_at") or added_at)
            except (TypeError, ValueError):
                updated_at = added_at
            raw_sources = raw_item.get("source_paths")
            source_paths = [
                str(p).strip() for p in raw_sources if str(p).strip()
            ] if isinstance(raw_sources, list) else []
            items.append({
                "id": str(raw_item.get("id") or uuid.uuid4().hex),
                "source_path": source,
                "source_paths": source_paths,
                "pre_framed": bool(raw_item.get("pre_framed")),
                "name": str(raw_item.get("name") or Path(source).name),
                "status": status,
                "added_at": added_at,
                "updated_at": updated_at,
                "error": str(raw_item.get("error") or ""),
            })
        return items

    def save_queue(self):
        DATA_ROOT.mkdir(parents=True, exist_ok=True)
        QUEUE_FILE.write_text(json.dumps(self.queue_items, indent=2), encoding="utf-8")

    def load_watch_folder(self) -> Path | None:
        if not WATCH_FILE.exists():
            return None
        try:
            data = json.loads(WATCH_FILE.read_text(encoding="utf-8"))
        except Exception:
            return None
        folder = str(data.get("folder") or "").strip() if isinstance(data, dict) else ""
        return Path(folder) if folder else None

    def save_watch_folder(self):
        DATA_ROOT.mkdir(parents=True, exist_ok=True)
        payload = {"folder": str(self.watch_folder) if self.watch_folder else ""}
        WATCH_FILE.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    def queue_counts(self) -> dict[str, int]:
        counts = {status: 0 for status in QUEUE_STATUSES}
        for item in self.queue_items:
            counts[item.get("status", "pending")] = counts.get(item.get("status", "pending"), 0) + 1
        return counts

    def pending_queue_items(self) -> list[dict]:
        return [item for item in self.queue_items if item.get("status") == "pending"]

    def update_queue_item(self, item_id: str, status: str, error: str = ""):
        for item in self.queue_items:
            if item.get("id") == item_id:
                item["status"] = status
                item["updated_at"] = time.time()
                item["error"] = error
                break
        self.save_queue()
        self.root.after(0, self.render_queue)

    def render_queue(self):
        if not hasattr(self, "image_list"):
            return
        self.image_list.delete(0, "end")
        if not self.queue_items:
            self.image_list.insert("end", "No artwork queued.")
        else:
            for item in self.queue_items:
                label = item.get("status", "pending").upper()
                name = item.get("name") or Path(item.get("source_path", "")).name
                suffix = f" - {item.get('error')[:90]}" if item.get("status") == "failed" and item.get("error") else ""
                self.image_list.insert("end", f"[{label}] {name}{suffix}")
        self.update_queue_status()

    def update_queue_status(self):
        if not hasattr(self, "work_status"):
            return
        counts = self.queue_counts()
        total = len(self.queue_items)
        status = (
            f"Queue: {counts.get('pending', 0)} pending, {counts.get('processing', 0)} processing, "
            f"{counts.get('done', 0)} done, {counts.get('failed', 0)} failed"
        )
        if total == 0:
            status = "Queue empty"
        if self.processing and self.pause_requested:
            status += " - pausing after current item"
        self.work_status.configure(text=status)
        can_start = self.connected and not self.processing and bool(self.pending_queue_items())
        if hasattr(self, "start_button"):
            self.start_button.configure(state="normal" if can_start else "disabled")

    def on_profile_selected(self, _event=None):
        profile = self.profile_var.get().strip()
        if not profile:
            return
        self.settings["PROFILE_NAME"] = profile
        save_app_settings(self.settings)
        apply_app_settings(self.settings)
        self.log(f"Profile selected: {profile}")

    def pre_framed_enabled(self) -> bool:
        return bool(self.pre_framed_var.get()) if hasattr(self, "pre_framed_var") else False

    def images_per_listing(self) -> int:
        try:
            value = int(self.images_per_listing_var.get())
        except (ValueError, tk.TclError):
            value = 1
        return max(1, min(20, value))

    def analysis_image_index(self) -> int:
        try:
            value = int(self.analysis_index_var.get())
        except (ValueError, tk.TclError):
            value = 1
        return max(1, min(20, value))

    def on_pre_framed_changed(self):
        self.settings["PRE_FRAMED_MODE"] = "true" if self.pre_framed_enabled() else "false"
        self.settings["IMAGES_PER_LISTING"] = str(self.images_per_listing())
        self.settings["ANALYSIS_IMAGE_INDEX"] = str(self.analysis_image_index())
        save_app_settings(self.settings)
        apply_app_settings(self.settings)
        if self.pre_framed_enabled():
            self.image_drop.configure(text=f"Drop FINISHED framed images here ({self.images_per_listing()} per listing)")
        else:
            self.image_drop.configure(text="Drop raw artwork images here")
        self.update_queue_status()

    def refresh_connection_async(self):
        if not config("LOCAL_WORKER_TOKEN"):
            self._set_connection_status("red", "Setup needed", "Open Settings")
            return
        if self.connection_check_in_flight:
            return
        self.connection_check_in_flight = True
        if not self.connected:
            self._set_connection_status("orange", "Connecting", "Checking Render")
        threading.Thread(target=self._check_connection, daemon=True).start()

    def _connection_loop(self):
        if not self.processing:
            self.refresh_connection_async()
        self.root.after(60000, self._connection_loop)

    def _check_connection(self):
        base_url = config("LISTING_CANNON_URL", "https://listing-cannon.onrender.com").rstrip("/")
        token = config("LOCAL_WORKER_TOKEN")
        try:
            response = requests.get(
                f"{base_url}/api/local_worker/health",
                headers={"Authorization": f"Bearer {token}"},
                # Short timeout so a slow/wedged server is detected quickly and the
                # next 60s loop can recover the moment it's healthy again, instead
                # of the UI sitting on "Connecting" for a full minute each cycle.
                timeout=90,
            )
            payload = response.json() if response.content else {}
            if response.ok and payload.get("connected") and payload.get("gemini_configured"):
                self.connection_failure_count = 0
                self.profiles = payload.get("profiles") or []
                shop = payload.get("shop_domain") or "Shop connected"
                self.root.after(0, lambda: self._apply_profiles(payload))
                self.root.after(0, lambda: self._set_connection_status("green", "Connected", shop))
            else:
                err = payload.get("error") or "Render reachable but not ready"
                self._record_connection_failure(err)
        except Exception as exc:
            self._record_connection_failure(str(exc)[:90])
        finally:
            self.connection_check_in_flight = False

    def _record_connection_failure(self, detail: str):
        self.connection_failure_count += 1
        if not self.connected and self.connection_failure_count < 3:
            attempt = self.connection_failure_count
            self.root.after(
                0,
                lambda: self._set_connection_status(
                    'orange', 'Waking Render', f'Cold-start retry {attempt}/3'
                ),
            )
            self.root.after(5000, self.refresh_connection_async)
            return
        if self.connected and self.connection_failure_count < 3:
            self.root.after(
                0,
                lambda: self._set_connection_status(
                    "green",
                    "Connected",
                    f"Render check delayed ({self.connection_failure_count}/3)",
                ),
            )
            return
        self.root.after(0, lambda: self._set_connection_status("red", "Offline", detail))

    def _apply_profiles(self, payload: dict):
        profiles = payload.get("profiles") or []
        if not profiles:
            return
        current = self.profile_var.get() or payload.get("configured_profile") or config("PROFILE_NAME", "MAIN 1")
        self.profile_combo.configure(values=profiles)
        if current in profiles:
            self.profile_var.set(current)
        else:
            self.profile_var.set(profiles[0])
            self.on_profile_selected()

    def load_frames(self):
        frames = sorted_frames(self.dirs["frames"])
        saved = self._read_frame_order()
        if saved:
            by_name = {path.name: path for path in frames}
            ordered = [by_name[name] for name in saved if name in by_name]
            ordered.extend([path for path in frames if path.name not in saved])
            frames = ordered
        self.frame_paths = frames
        self.render_frames()

    def _read_frame_order(self):
        try:
            data = json.loads(FRAME_ORDER_FILE.read_text(encoding="utf-8"))
            return data if isinstance(data, list) else []
        except Exception:
            return []

    def save_frame_order(self):
        FRAME_ORDER_FILE.write_text(json.dumps([path.name for path in self.frame_paths], indent=2), encoding="utf-8")

    def render_frames(self):
        self.frames_list.delete(0, "end")
        if not self.frame_paths:
            self.frames_list.insert("end", "No PSD/PSB frames added.")
            return
        for index, path in enumerate(self.frame_paths, start=1):
            self.frames_list.insert("end", f"{index}. {path.name}     X")

    def add_frames(self):
        paths = filedialog.askopenfilenames(title="Choose PSD mockups", filetypes=[("PSD files", "*.psd *.psb"), ("All files", "*.*")])
        self.add_frame_paths(paths)

    def on_frame_drop(self, event):
        self.add_frame_paths(self.root.tk.splitlist(event.data))

    def add_frame_paths(self, paths):
        added = 0
        existing = {path.name for path in self.frame_paths}
        for raw_path in paths:
            source = Path(raw_path)
            if not source.is_file() or source.suffix.lower() not in PSD_EXTENSIONS:
                continue
            dest = self.dirs["frames"] / source.name
            if source.resolve() != dest.resolve():
                dest = unique_path(dest)
                shutil.copy2(source, dest)
            if dest.name not in existing:
                self.frame_paths.append(dest)
                existing.add(dest.name)
                added += 1
        if added:
            self.save_frame_order()
            self.render_frames()
            self.log(f"Added {added} PSD mockup(s)")

    def _frame_drag_start(self, event):
        self.drag_start_index = self.frames_list.nearest(event.y)

    def _frame_click_release(self, event):
        if event.x < self.frames_list.winfo_width() - 42:
            return
        index = self.frames_list.nearest(event.y)
        if 0 <= index < len(self.frame_paths):
            self.frames_list.selection_clear(0, "end")
            self.frames_list.selection_set(index)
            self.delete_selected_frame()

    def _frame_drag_motion(self, event):
        if self.drag_start_index is None or not self.frame_paths:
            return
        new_index = self.frames_list.nearest(event.y)
        if new_index == self.drag_start_index or new_index < 0 or new_index >= len(self.frame_paths):
            return
        item = self.frame_paths.pop(self.drag_start_index)
        self.frame_paths.insert(new_index, item)
        self.drag_start_index = new_index
        self.save_frame_order()
        self.render_frames()
        self.frames_list.selection_set(new_index)

    def selected_frame_index(self):
        selection = self.frames_list.curselection()
        if not selection:
            return None
        index = selection[0]
        return index if index < len(self.frame_paths) else None

    def move_frame(self, direction: int):
        index = self.selected_frame_index()
        if index is None:
            return
        new_index = index + direction
        if new_index < 0 or new_index >= len(self.frame_paths):
            return
        self.frame_paths[index], self.frame_paths[new_index] = self.frame_paths[new_index], self.frame_paths[index]
        self.save_frame_order()
        self.render_frames()
        self.frames_list.selection_set(new_index)

    def delete_selected_frame(self):
        index = self.selected_frame_index()
        if index is None:
            return
        path = self.frame_paths[index]
        if not messagebox.askyesno("Delete PSD Mockup", f"Remove this mockup from the app?\n\n{path.name}"):
            return
        self.frame_paths.pop(index)
        try:
            path.unlink()
        except OSError as exc:
            messagebox.showwarning("Could Not Delete File", str(exc))
        self.save_frame_order()
        self.render_frames()
        self.log(f"Deleted PSD mockup: {path.name}")

    def open_frames_folder(self):
        os.startfile(self.dirs["frames"])

    def add_images(self):
        paths = filedialog.askopenfilenames(
            title="Choose raw artwork images",
            filetypes=[("Image files", "*.jpg *.jpeg *.png *.webp *.bmp *.tif *.tiff"), ("All files", "*.*")],
        )
        self.add_image_paths(paths)

    def add_image_folder(self):
        folder = filedialog.askdirectory(title="Choose folder of raw artwork images")
        if not folder:
            return
        root = Path(folder)
        paths = self.scan_image_folder(root)
        self.add_image_paths(paths, source_label=root.name)

    def choose_watch_folder(self):
        if self.watch_folder:
            stop = messagebox.askyesnocancel(
                "Watch Folder",
                f"Currently watching:\n{self.watch_folder}\n\nYes = stop watching\nNo = choose a different folder",
            )
            if stop is None:
                return
            if stop:
                self.watch_folder = None
                self.save_watch_folder()
                self.log("Stopped watching folder.")
                return
        folder = filedialog.askdirectory(title="Choose folder to watch for raw artwork images")
        if not folder:
            return
        self.watch_folder = Path(folder)
        self.save_watch_folder()
        self.log(f"Watching folder: {self.watch_folder}")
        self.add_image_paths(self.scan_image_folder(self.watch_folder), source_label=f"watch folder {self.watch_folder.name}")

    def scan_image_folder(self, root: Path, require_settled: bool = False) -> list[Path]:
        if not root.exists() or not root.is_dir():
            return []
        now = time.time()
        paths = []
        for path in root.rglob("*"):
            if not path.is_file() or path.suffix.lower() not in IMAGE_EXTENSIONS:
                continue
            if require_settled:
                try:
                    if now - path.stat().st_mtime < WATCH_MIN_AGE_SECONDS:
                        continue
                except OSError:
                    continue
            paths.append(path)
        return paths

    def _watch_loop(self):
        if self.watch_folder and self.watch_folder.exists():
            self.add_image_paths(
                self.scan_image_folder(self.watch_folder, require_settled=True),
                source_label=f"watch folder {self.watch_folder.name}",
                quiet=True,
            )
        self.root.after(WATCH_SCAN_SECONDS * 1000, self._watch_loop)

    def on_image_drop(self, event):
        self.add_image_paths(self.root.tk.splitlist(event.data))

    def add_image_paths(self, paths, source_label: str = "selection", quiet: bool = False):
        added = 0
        skipped = 0
        # Only treat a design as a duplicate if it's still ACTIVE in the queue
        # (pending/processing). Designs that already finished or failed can be
        # re-dropped to process again — you should be able to run the same
        # artwork twice, and a previous failure must never block a retry.
        existing_sources = set()
        for item in self.queue_items:
            if item.get("status") not in ("pending", "processing"):
                continue
            for source in [item.get("source_path"), *(item.get("source_paths") or [])]:
                if source:
                    existing_sources.add(str(Path(source).resolve()).lower())
        new_paths: list[Path] = []
        for raw_path in paths:
            path = Path(raw_path)
            if not path.is_file() or path.suffix.lower() not in IMAGE_EXTENSIONS:
                continue
            resolved = str(path.resolve()).lower()
            if resolved in existing_sources:
                skipped += 1
                continue
            existing_sources.add(resolved)
            new_paths.append(path.resolve())

        if self.pre_framed_enabled():
            # Pre-framed mode: images of one design live together, so chunk into
            # groups of N WITHIN each parent folder (sorted by filename). A folder
            # with a wrong count only loses its own remainder — it can never shift
            # the grouping and mix designs from neighbouring folders into one
            # listing, which would silently create wrong products.
            group_size = self.images_per_listing()
            by_folder: dict[str, list[Path]] = {}
            for path in new_paths:
                by_folder.setdefault(str(path.parent).lower(), []).append(path)
            leftover_paths: list[Path] = []
            for folder_key in sorted(by_folder):
                folder_paths = sorted(by_folder[folder_key], key=lambda p: p.name.lower())
                cut = len(folder_paths) - (len(folder_paths) % group_size)
                leftover_paths.extend(folder_paths[cut:])
                for start in range(0, cut, group_size):
                    group = folder_paths[start:start + group_size]
                    extra = f" (+{len(group) - 1} more)" if len(group) > 1 else ""
                    self.queue_items.append({
                        "id": uuid.uuid4().hex,
                        "source_path": str(group[0]),
                        "source_paths": [str(p) for p in group],
                        "pre_framed": True,
                        "name": f"{group[0].parent.name}\\{group[0].name}{extra}",
                        "status": "pending",
                        "added_at": time.time(),
                        "updated_at": time.time(),
                        "error": "",
                    })
                    added += 1
            if leftover_paths:
                names = ", ".join(p.name for p in leftover_paths[:5])
                self.log(
                    f"Pre-framed mode: {len(leftover_paths)} image(s) left over (their folder "
                    f"is not a multiple of {group_size} per listing) — NOT queued: {names}"
                )
                if not quiet:
                    messagebox.showwarning(
                        "Images left over",
                        f"{len(leftover_paths)} image(s) were not queued because their folder "
                        f"does not hold a multiple of {group_size} images per listing.\n\n"
                        f"First leftover: {leftover_paths[0]}",
                    )
            unit = "listing group(s)"
        else:
            for path in new_paths:
                self.queue_items.append({
                    "id": uuid.uuid4().hex,
                    "source_path": str(path),
                    "source_paths": [],
                    "pre_framed": False,
                    "name": path.name,
                    "status": "pending",
                    "added_at": time.time(),
                    "updated_at": time.time(),
                    "error": "",
                })
                added += 1
            unit = "image(s)"
        if added:
            self.save_queue()
            self.render_queue()
        if not quiet or added:
            self.log(f"Imported {added} {unit} from {source_label}; skipped {skipped} already queued/completed.")

    def clear_images(self):
        if self.processing:
            return
        if self.queue_items and not messagebox.askyesno("Clear Queue", "Remove all queued artwork history from this PC?"):
            return
        self.queue_items.clear()
        self.save_queue()
        self.render_queue()
        self.log("Queue cleared.")

    def clear_done(self):
        if self.processing:
            return
        before = len(self.queue_items)
        self.queue_items = [item for item in self.queue_items if item.get("status") != "done"]
        removed = before - len(self.queue_items)
        self.save_queue()
        self.render_queue()
        self.log(f"Cleared {removed} completed queue item(s).")

    def retry_failed(self):
        if self.processing:
            return
        count = 0
        for item in self.queue_items:
            if item.get("status") == "failed":
                item["status"] = "pending"
                item["updated_at"] = time.time()
                item["error"] = ""
                count += 1
        self.save_queue()
        self.render_queue()
        self.log(f"Requeued {count} failed item(s).")

    def pause_processing(self):
        if not self.processing:
            return
        self.pause_requested = True
        self.pause_button.configure(state="disabled")
        self.log("Pause requested. Current artwork will finish first.")
        self.update_queue_status()

    def start_processing(self):
        if self.processing or not self.connected:
            return
        pending = self.pending_queue_items()
        needs_frames = any(not item.get("pre_framed") for item in pending)
        if needs_frames and not self.frame_paths:
            messagebox.showerror("No PSD Mockups", "Drop PSD/PSB mockups into the left panel first (or enable pre-framed mode before adding images).")
            return
        if not pending:
            messagebox.showerror("No Artwork", "Drop raw artwork images first.")
            return
        profile = self.profile_var.get().strip()
        if not profile:
            messagebox.showerror("No Profile", "Choose a saved Listing Cannon profile.")
            return
        self.processing = True
        self.pause_requested = False
        self.batch_started_at = time.time()
        self.batch_listed_count = 0
        self.start_button.configure(state="disabled")
        self.pause_button.configure(state="normal")
        threading.Thread(target=self._process_queue, args=(profile,), daemon=True).start()
        self._tick_batch_stats()

    @staticmethod
    def _fmt_duration(seconds: float) -> str:
        seconds = max(0, int(seconds))
        hours, rem = divmod(seconds, 3600)
        minutes, secs = divmod(rem, 60)
        if hours:
            return f"{hours}h {minutes}m {secs}s"
        return f"{minutes}m {secs}s"

    def _tick_batch_stats(self):
        """Refresh the live batch stats line once a second while processing."""
        if self.batch_started_at is None:
            return
        elapsed = time.time() - self.batch_started_at
        listed = self.batch_listed_count
        text = f"Active {self._fmt_duration(elapsed)}  |  {listed} listing(s) added"
        if listed > 0:
            text += f"  |  avg {self._fmt_duration(elapsed / listed)} per listing"
        if not self.processing:
            text = "Batch finished — " + text.replace("Active", "ran")
        if hasattr(self, "batch_stats"):
            self.batch_stats.configure(text=text)
        if self.processing:
            self.root.after(1000, self._tick_batch_stats)

    def _ensure_source_available(self, source_path: Path, attempts: int = 8, delay: float = 4.0) -> bool:
        """Return True once *source_path* is genuinely readable.

        Cloud-synced files (Google Drive / OneDrive 'Files On-Demand') are
        placeholders that only download when opened, and providers throttle or
        briefly drop access during long batches. A single exists() check gives
        false 'missing' failures, so retry and force a hydration read first."""
        for attempt in range(1, attempts + 1):
            try:
                if source_path.exists() and source_path.stat().st_size > 0:
                    with open(source_path, "rb") as fh:  # forces cloud hydration
                        fh.read(1)
                    return True
            except OSError:
                pass
            if attempt < attempts:
                self.log(
                    f"Source not ready ({source_path.name}); retry {attempt}/{attempts} in {int(delay)}s "
                    f"(cloud drive hydrating?)"
                )
                time.sleep(delay)
        return False

    def _copy_source_with_retry(self, source_path: Path, inbox_path: Path, attempts: int = 4, delay: float = 3.0):
        """Copy source into the inbox, tolerating transient cloud-drive IO errors."""
        last_error: Exception | None = None
        for attempt in range(1, attempts + 1):
            try:
                shutil.copy2(source_path, inbox_path)
                return
            except OSError as exc:
                last_error = exc
                if attempt < attempts:
                    self.log(f"Copy retry {attempt}/{attempts} for {source_path.name}: {exc}")
                    time.sleep(delay)
        raise last_error if last_error else OSError("copy failed")

    def _report_batch_outcome(self):
        """Write a failed-items report and clearly notify the user at batch end."""
        counts = self.queue_counts()
        done = counts.get("done", 0)
        failed = counts.get("failed", 0)
        failed_items = [it for it in self.queue_items if it.get("status") == "failed"]
        self.log(f"BATCH STOPPED — {done} listed, {failed} failed.")
        if not failed_items:
            return
        report_path = None
        try:
            report_path = DATA_ROOT / "failed_report.txt"
            lines = [
                "Listing Cannon — failed items report",
                time.strftime("%Y-%m-%d %H:%M:%S"),
                f"{failed} failed, {done} listed",
                "",
            ]
            for it in failed_items:
                name = it.get("name") or Path(it.get("source_path", "")).name
                lines.append(f"- {name}")
                lines.append(f"    reason: {it.get('error') or 'unknown'}")
                lines.append(f"    source: {it.get('source_path', '')}")
            report_path.write_text("\n".join(lines), encoding="utf-8")
            self.log(f"Failed-items report written: {report_path}")
        except Exception as exc:
            self.log(f"Could not write failed report: {exc}")
            report_path = None
        msg = (
            f"{failed} item(s) did not list.\n"
            f"{done} listed successfully.\n\n"
            "Click 'Retry Failed' to requeue them, then 'Frame and Send' again."
        )
        if report_path:
            msg += f"\n\nDetails saved to:\n{report_path}"
        try:
            messagebox.showwarning("Batch finished with failures", msg)
        except Exception:
            pass

    def _prevent_sleep(self, enable: bool):
        """Keep Windows fully awake for the duration of a batch so the run never
        gets suspended. Holds BOTH system and display: on Modern Standby PCs the
        screen timing out is what drops the machine into standby and freezes the
        batch, so the display must be kept on too. Uses SetThreadExecutionState
        on the calling (worker) thread. Called with True at batch start and
        False at batch end."""
        if sys.platform != "win32":
            return
        try:
            import ctypes
            ES_CONTINUOUS = 0x80000000
            ES_SYSTEM_REQUIRED = 0x00000001
            ES_DISPLAY_REQUIRED = 0x00000002
            ES_AWAYMODE_REQUIRED = 0x00000040
            if enable:
                flags = ES_CONTINUOUS | ES_SYSTEM_REQUIRED | ES_DISPLAY_REQUIRED | ES_AWAYMODE_REQUIRED
                if ctypes.windll.kernel32.SetThreadExecutionState(flags) == 0:
                    # Away mode unsupported here; still hold system + display.
                    ctypes.windll.kernel32.SetThreadExecutionState(
                        ES_CONTINUOUS | ES_SYSTEM_REQUIRED | ES_DISPLAY_REQUIRED
                    )
                if not getattr(self, "_keep_awake_logged", False):
                    self._keep_awake_logged = True
                    self.log(
                        "Keeping PC and screen awake while listing (neither will "
                        "sleep until the batch finishes)."
                    )
            else:
                self._keep_awake_logged = False
                ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS)
        except Exception as exc:
            self.log(f"Could not change sleep prevention: {exc}")

    @staticmethod
    def _discard_staged(staged_path: Path) -> None:
        """Remove a staged pre-render working copy — a single file for normal
        items, a whole group folder for pre-framed items."""
        if staged_path.is_dir():
            shutil.rmtree(staged_path, ignore_errors=True)
        else:
            staged_path.unlink(missing_ok=True)

    def _prepare_item(self, item: dict, frames: list[Path]) -> tuple[Path, list[Path], Path]:
        """Local half of one queue item: fetch the source and render all frames.

        Pre-framed groups skip PSD rendering entirely: the images are staged
        as-is and one of them becomes the AI analysis image.
        Runs on the pre-render executor so the NEXT artwork's frames can render
        while the current one is waiting on Render. Raises on any failure."""
        cloud_hint = (
            "Source file unavailable after retries (cloud drive offline/throttled?). "
            "If it lives on Google Drive/OneDrive, right-click the folder and set "
            "'Available offline' so files aren't cloud placeholders."
        )
        if item.get("pre_framed"):
            source_paths = [Path(p) for p in (item.get("source_paths") or [item["source_path"]])]
            for source_path in source_paths:
                if not self._ensure_source_available(source_path):
                    raise RuntimeError(f"{source_path.name}: {cloud_hint}")
            return prepare_ready_group(
                source_paths,
                self.dirs,
                analysis_index=self.analysis_image_index(),
                log=self.log,
            )
        source_path = Path(item["source_path"])
        if not self._ensure_source_available(source_path):
            raise RuntimeError(cloud_hint)
        inbox_path = unique_path(self.dirs["inbox"] / source_path.name)
        self._copy_source_with_retry(source_path, inbox_path)
        self.log(f"Queued local copy: {inbox_path.name}")
        return render_stage(inbox_path, frames, self.dirs, log=self.log)

    def _process_queue(self, profile: str):
        self._prevent_sleep(True)
        # One background renderer overlaps the next artwork's local PSD render
        # with the current artwork's Render wait (the slow network half).
        executor = ThreadPoolExecutor(max_workers=1)
        prerender: tuple[str, object] | None = None  # (item_id, Future)
        try:
            frames = list(self.frame_paths)
            while True:
                self._prevent_sleep(True)  # re-assert each item; cheap insurance
                if self.pause_requested:
                    self.log("Batch paused.")
                    break
                pending = self.pending_queue_items()
                if not pending:
                    self.log("All pending images processed.")
                    break
                counts = self.queue_counts()
                done_before = counts.get("done", 0) + counts.get("failed", 0)
                total = len(self.queue_items)
                item = pending[0]
                source_path = Path(item["source_path"])
                self.update_queue_item(item["id"], "processing")
                self.root.after(
                    0,
                    lambda d=done_before + 1, t=total, n=source_path.name: self.work_status.configure(text=f"Processing {d}/{t}: {n}"),
                )
                # Reuse the pre-render started during the previous item's wait,
                # if it was for this item; otherwise render now.
                future = None
                if prerender is not None:
                    if prerender[0] == item["id"]:
                        future = prerender[1]
                    else:
                        # Queue order changed; discard the stale pre-render.
                        try:
                            self._discard_staged(prerender[1].result()[0])
                        except Exception:
                            pass
                prerender = None
                try:
                    if future is None:
                        future = executor.submit(self._prepare_item, item, frames)
                    staged = future.result()
                    # Current item is rendered; start rendering the next one in
                    # the background while we upload + wait on Render.
                    next_item = next(
                        (it for it in self.pending_queue_items() if it["id"] != item["id"]),
                        None,
                    )
                    if next_item is not None and not self.pause_requested:
                        prerender = (next_item["id"], executor.submit(self._prepare_item, next_item, frames))
                    extra_form = {"analysis_is_framed": "true"} if item.get("pre_framed") else None
                    ok = publish_stage(*staged, self.dirs, log=self.log, profile_name=profile, extra_form=extra_form)
                    if ok:
                        self.batch_listed_count += 1
                        self.update_queue_item(item["id"], "done")
                    else:
                        self.update_queue_item(item["id"], "failed", "Processing failed. See the log and failed folder for details.")
                        self._halt_batch(source_path.name, "Processing failed. See the log for details.")
                        break
                except Exception as exc:
                    self.update_queue_item(item["id"], "failed", str(exc)[:300])
                    self.log(f"FAILED {source_path.name}: {exc}")
                    self._halt_batch(source_path.name, str(exc)[:300])
                    break
                if self.pending_queue_items() and not self.pause_requested:
                    self.log(f"Throttling {BATCH_DELAY_SECONDS}s before next artwork.")
                    time.sleep(BATCH_DELAY_SECONDS)
        finally:
            # If a pre-render is still staged when the batch stops (pause/finish),
            # discard its local working copy — the item stays "pending" and will
            # re-render from the original source next run.
            if prerender is not None:
                try:
                    self._discard_staged(prerender[1].result()[0])
                except Exception:
                    pass
            executor.shutdown(wait=False)
            self._prevent_sleep(False)
            self.processing = False
            self.root.after(0, self._tick_batch_stats)  # final "Batch finished" stats
            self.root.after(0, lambda: self.pause_button.configure(state="disabled"))
            self.root.after(0, self.update_queue_status)
            self.root.after(0, self._report_batch_outcome)

    def _halt_batch(self, artwork_name: str, reason: str):
        """Stop the batch on the first failure and put it in front of the user.

        Nothing is published for a failed artwork, and continuing would hide the
        problem behind a long run of later listings.
        """
        self.pause_requested = True
        for line in (
            "",
            "=" * 70,
            "PROCESSING STOPPED - nothing was published for this artwork.",
            "Artwork: " + artwork_name,
            "Reason: " + reason,
            "The remaining images stay queued. Fix the cause, then press Start again.",
            "=" * 70,
        ):
            self.log(line)
        message = (
            "Nothing was published for " + artwork_name + "."
            + chr(10) + chr(10) + reason + chr(10) + chr(10)
            + "The batch has been stopped so this can be checked. "
            + "The remaining images are still queued."
        )
        self.root.after(
            0,
            lambda: messagebox.showerror("Listing Cannon - processing stopped", message),
        )


    def log(self, message: str):
        self.log_queue.put(message)

    def _drain_logs(self):
        try:
            while True:
                message = self.log_queue.get_nowait()
                self.log_text.insert("end", message + "\n")
                self.log_text.see("end")
        except queue.Empty:
            pass
        self.root.after(150, self._drain_logs)


def main():
    root_class = TkinterDnD.Tk if TkinterDnD else tk.Tk
    root = root_class()
    ListingCannonDesktop(root)
    root.mainloop()


if __name__ == "__main__":
    main()
