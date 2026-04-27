"""
settings_modal.py — Pyregon Settings Modal Overlay
Gear-icon triggered, touch-friendly, write-through to config.
"""

import tkinter as tk
from tkinter import ttk, messagebox
import logging
from config import config, SETTINGS_SCHEMA

logger = logging.getLogger(__name__)

# ── Visual constants (match existing control panel palette) ─────────────────
BG_DARK      = "#0d1117"
BG_PANEL     = "#161b22"
BG_SECTION   = "#1c2128"
BG_INPUT     = "#21262d"
ACCENT_RED   = "#f85149"
ACCENT_AMBER = "#f0883e"
ACCENT_GREEN = "#3fb950"
ACCENT_BLUE  = "#58a6ff"
TEXT_PRIMARY = "#e6edf3"
TEXT_MUTED   = "#7d8590"
BORDER       = "#30363d"
FONT_MONO    = ("Courier New", 11)
FONT_LABEL   = ("Helvetica", 11)
FONT_SECTION = ("Helvetica", 12, "bold")
FONT_TITLE   = ("Helvetica", 15, "bold")
FONT_GEAR    = ("Helvetica", 18)


class SettingsModal:
    """
    Modal settings overlay. Attach to any Tkinter root or frame.

    Usage:
        settings_btn = SettingsModal.create_gear_button(parent, root)
    """

    def __init__(self, root):
        self.root = root
        self._widgets = {}  # key → (variable, entry/spinbox widget)
        self._window = None

    # ── Gear Button Factory ───────────────────────────────────────────────────

    @staticmethod
    def create_gear_button(parent, root):
        """Create and return a gear button that opens the settings modal."""
        modal = SettingsModal(root)
        btn = tk.Button(
            parent,
            text="⚙",
            font=FONT_GEAR,
            bg=BG_DARK,
            fg=TEXT_MUTED,
            activebackground=BG_PANEL,
            activeforeground=TEXT_PRIMARY,
            bd=0,
            padx=6,
            pady=2,
            cursor="hand2",
            command=modal.open,
        )
        return btn, modal

    # ── Open / Close ──────────────────────────────────────────────────────────

    def open(self):
        if self._window and self._window.winfo_exists():
            self._window.lift()
            return

        self._window = tk.Toplevel(self.root)
        self._window.title("Pyregon Settings")
        self._window.configure(bg=BG_DARK)
        self._window.transient(self.root)
        self._window.grab_set()
        self._window.resizable(False, False)

        # Center over root
        self.root.update_idletasks()
        rw, rh = self.root.winfo_width(), self.root.winfo_height()
        rx, ry = self.root.winfo_rootx(), self.root.winfo_rooty()
        w, h = 560, min(680, rh - 40)
        x = rx + (rw - w) // 2
        y = ry + (rh - h) // 2
        self._window.geometry(f"{w}x{h}+{x}+{y}")

        self._build_ui(w, h)
        self._window.protocol("WM_DELETE_WINDOW", self._on_close)

    def _on_close(self):
        if self._window:
            self._window.grab_release()
            self._window.destroy()
            self._window = None

    # ── UI Construction ───────────────────────────────────────────────────────

    def _build_ui(self, w, h):
        win = self._window

        # ── Header ────────────────────────────────────────────────────────────
        header = tk.Frame(win, bg=BG_PANEL, pady=10)
        header.pack(fill="x", padx=0)

        tk.Label(header, text="⚙  SYSTEM SETTINGS", font=FONT_TITLE,
                 bg=BG_PANEL, fg=TEXT_PRIMARY).pack(side="left", padx=16)

        tk.Button(header, text="✕", font=("Helvetica", 13), bg=BG_PANEL,
                  fg=TEXT_MUTED, activebackground=ACCENT_RED,
                  activeforeground="white", bd=0, padx=8,
                  command=self._on_close).pack(side="right", padx=8)

        # Separator
        tk.Frame(win, bg=BORDER, height=1).pack(fill="x")

        # ── Scrollable body ───────────────────────────────────────────────────
        canvas = tk.Canvas(win, bg=BG_DARK, highlightthickness=0,
                           height=h - 130)
        scrollbar = ttk.Scrollbar(win, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True)

        body = tk.Frame(canvas, bg=BG_DARK)
        body_window = canvas.create_window((0, 0), window=body, anchor="nw")

        def on_configure(event):
            canvas.configure(scrollregion=canvas.bbox("all"))
            canvas.itemconfig(body_window, width=canvas.winfo_width())

        body.bind("<Configure>", on_configure)
        canvas.bind("<Configure>", lambda e: canvas.itemconfig(
            body_window, width=canvas.winfo_width()))

        # Touch scroll
        canvas.bind_all("<MouseWheel>", lambda e: canvas.yview_scroll(
            int(-1 * (e.delta / 120)), "units"))

        # ── Settings sections ──────────────────────────────────────────────────
        self._widgets.clear()
        for section_name, fields in SETTINGS_SCHEMA.items():
            self._build_section(body, section_name, fields)

        # ── Footer ────────────────────────────────────────────────────────────
        tk.Frame(win, bg=BORDER, height=1).pack(fill="x", side="bottom")
        footer = tk.Frame(win, bg=BG_PANEL, pady=10)
        footer.pack(fill="x", side="bottom")

        tk.Button(footer, text="Reset to Defaults", font=FONT_LABEL,
                  bg=BG_INPUT, fg=ACCENT_AMBER, activebackground=BG_SECTION,
                  activeforeground=ACCENT_AMBER, bd=0, padx=12, pady=6,
                  cursor="hand2", command=self._reset_defaults
                  ).pack(side="left", padx=16)

        tk.Button(footer, text="Save & Close", font=("Helvetica", 11, "bold"),
                  bg=ACCENT_GREEN, fg=BG_DARK, activebackground="#2ea043",
                  activeforeground=BG_DARK, bd=0, padx=16, pady=6,
                  cursor="hand2", command=self._save_and_close
                  ).pack(side="right", padx=16)

        tk.Button(footer, text="Cancel", font=FONT_LABEL,
                  bg=BG_INPUT, fg=TEXT_MUTED, activebackground=BG_SECTION,
                  activeforeground=TEXT_PRIMARY, bd=0, padx=12, pady=6,
                  cursor="hand2", command=self._on_close
                  ).pack(side="right", padx=4)

    def _build_section(self, parent, title, fields):
        # Section header
        sec_frame = tk.Frame(parent, bg=BG_SECTION, pady=6)
        sec_frame.pack(fill="x", padx=12, pady=(10, 2))
        tk.Label(sec_frame, text=title.upper(), font=FONT_SECTION,
                 bg=BG_SECTION, fg=ACCENT_BLUE, padx=10).pack(anchor="w")

        # Fields
        for key, label, unit, input_type, vmin, vmax, step in fields:
            self._build_field(parent, key, label, unit, input_type,
                              vmin, vmax, step)

    def _build_field(self, parent, key, label, unit, input_type,
                     vmin, vmax, step):
        row = tk.Frame(parent, bg=BG_DARK, pady=4)
        row.pack(fill="x", padx=12)

        # Label column
        lbl_frame = tk.Frame(row, bg=BG_DARK, width=260)
        lbl_frame.pack(side="left", fill="y")
        lbl_frame.pack_propagate(False)
        tk.Label(lbl_frame, text=label, font=FONT_LABEL,
                 bg=BG_DARK, fg=TEXT_PRIMARY, anchor="w").pack(
            side="left", padx=(4, 0), fill="y")

        # Input column
        inp_frame = tk.Frame(row, bg=BG_DARK)
        inp_frame.pack(side="left", fill="x", expand=True)

        raw_val = config.get(key)

        # Convert internal seconds→minutes for "int_m" display type
        display_val = raw_val
        if input_type == "int_m" and raw_val is not None:
            display_val = raw_val // 60

        if input_type in ("int", "int_m"):
            var = tk.IntVar(value=display_val if display_val is not None else vmin)
        else:
            var = tk.DoubleVar(value=display_val if display_val is not None else vmin)

        spin = tk.Spinbox(
            inp_frame,
            from_=vmin, to=vmax, increment=step,
            textvariable=var,
            width=7,
            font=FONT_MONO,
            bg=BG_INPUT, fg=TEXT_PRIMARY,
            buttonbackground=BG_SECTION,
            insertbackground=TEXT_PRIMARY,
            relief="flat",
            bd=4,
        )
        spin.pack(side="left", padx=4)

        if unit:
            tk.Label(inp_frame, text=unit, font=FONT_LABEL,
                     bg=BG_DARK, fg=TEXT_MUTED).pack(side="left")

        self._widgets[key] = (var, spin, input_type)

        # Subtle separator
        tk.Frame(parent, bg=BORDER, height=1).pack(fill="x", padx=20)

    # ── Actions ───────────────────────────────────────────────────────────────

    def _save_and_close(self):
        """Validate all fields, write-through to config, close modal."""
        errors = []
        pending = {}

        for key, (var, widget, input_type) in self._widgets.items():
            try:
                raw = var.get()
                # Convert minutes back to seconds for int_m types
                if input_type == "int_m":
                    raw = int(raw) * 60
                elif input_type == "int":
                    raw = int(raw)
                else:
                    raw = round(float(raw), 2)
                pending[key] = raw
            except (tk.TclError, ValueError) as e:
                errors.append(f"Invalid value for '{key}': {e}")

        if errors:
            messagebox.showerror("Validation Error", "\n".join(errors),
                                 parent=self._window)
            return

        # Additional cross-field validation
        if "wind_speed_moderate" in pending and "wind_speed_high" in pending:
            if pending["wind_speed_moderate"] >= pending["wind_speed_high"]:
                messagebox.showerror(
                    "Validation Error",
                    "Moderate wind threshold must be less than High wind threshold.",
                    parent=self._window
                )
                return

        if "ember_clear_confidence" in pending and "ember_trigger_confidence" in pending:
            if pending["ember_clear_confidence"] >= pending["ember_trigger_confidence"]:
                messagebox.showerror(
                    "Validation Error",
                    "Ember clear confidence must be lower than trigger confidence.",
                    parent=self._window
                )
                return

        # Write all values
        for key, value in pending.items():
            config.set(key, value)

        logger.info(f"Settings saved: {pending}")
        self._on_close()

    def _reset_defaults(self):
        confirm = messagebox.askyesno(
            "Reset Settings",
            "Reset all settings to factory defaults?\nThis cannot be undone.",
            parent=self._window
        )
        if confirm:
            config.reset_to_defaults()
            # Reload UI values
            self._on_close()
            self.open()
            logger.info("Settings reset to defaults.")


# ── Standalone test ───────────────────────────────────────────────────────────

if __name__ == "__main__":
    root = tk.Tk()
    root.title("Pyregon Control Panel")
    root.configure(bg="#0d1117")
    root.geometry("800x480")

    top_bar = tk.Frame(root, bg="#161b22", pady=8)
    top_bar.pack(fill="x")
    tk.Label(top_bar, text="PYREGON CONTROL PANEL", font=("Helvetica", 14, "bold"),
             bg="#161b22", fg="#e6edf3").pack(side="left", padx=16)

    gear_btn, modal = SettingsModal.create_gear_button(top_bar, root)
    gear_btn.pack(side="right", padx=12)

    tk.Label(root, text="Main panel content here", bg="#0d1117",
             fg="#7d8590", font=("Helvetica", 12)).pack(expand=True)

    root.mainloop()
