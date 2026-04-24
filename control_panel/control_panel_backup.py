#!/usr/bin/env python3
"""
Wildfire Defense System - Control Panel v2
Optimized for 800x480 5" touchscreen on Raspberry Pi
"""

import tkinter as tk
from tkinter import messagebox
import threading
import time
import json
import os
import subprocess
import logging
from datetime import datetime
from collections import deque

# ─── Configuration ─────────────────────────────────────────────────────────────

DISPLAY_WIDTH  = 800
DISPLAY_HEIGHT = 480
FULLSCREEN     = False   # Set True when running on touchscreen only
SCREEN_OFFSET  = "+3840+0"  # Offset to 5" screen; set "+0+0" for single display

# Relay mapping (Sequent Microsystems 8-relay HAT, board=0)
RELAY_CHOKE_EXTEND   = 1   # Choke actuator extension
RELAY_STARTER        = 2   # Starter relay
RELAY_CHOKE_RETRACT  = 3   # Choke actuator retraction
RELAY_IGNITION_KILL  = 4   # Ignition coil shut-off (NO — energize to kill)
RELAY_VALVE_ZONE1    = 5   # Sprinkler zone 1
RELAY_VALVE_ZONE2    = 6   # Sprinkler zone 2
RELAY_VALVE_ZONE3    = 7   # Sprinkler zone 3
RELAY_VALVE_ZONE4    = 8   # Sprinkler zone 4

# Frigate NVR endpoint
FRIGATE_URL = "http://localhost:5000"

# Log file
LOG_FILE = "/var/log/wildfire_panel.log"
LOG_BUFFER_SIZE = 200

# ─── Logging ───────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE, mode="a"),
        logging.StreamHandler()
    ]
)
log = logging.getLogger("wildfire_panel")

# ─── Relay HAT ─────────────────────────────────────────────────────────────────

try:
    import smbus2
    HAT_AVAILABLE = True
    log.info("smbus2 found — relay HAT control enabled")
except ImportError:
    HAT_AVAILABLE = False
    log.warning("smbus2 not found — simulation mode")

HAT_BOARD = 0
_relay_state = {i: False for i in range(1, 9)}

def relay_set(relay_num: int, on: bool):
    global _relay_state
    _relay_state[relay_num] = on
    if HAT_AVAILABLE:
        try:
            import lib8relind
            lib8relind.set(HAT_BOARD, relay_num, 1 if on else 0)
        except Exception as e:
            log.error(f"Relay {relay_num} set failed: {e}")
    log.info(f"Relay {relay_num} → {'ON' if on else 'OFF'}")

def relay_get(relay_num: int) -> bool:
    return _relay_state.get(relay_num, False)

# ─── Engine sequencer ──────────────────────────────────────────────────────────

class EngineSequencer:
    CHOKE_EXTEND_TIME  = 3.0
    CRANK_TIME         = 3.0
    IDLE_WARM_TIME     = 5.0
    CHOKE_RETRACT_TIME = 3.0
    IGNITION_KILL_TIME = 3.0

    def __init__(self):
        self.running  = False
        self.starting = False
        self.stopping = False
        self._thread  = None

    def start(self, status_callback=None):
        if self.running or self.starting:
            return
        self._thread = threading.Thread(target=self._start_seq, args=(status_callback,), daemon=True)
        self._thread.start()

    def stop(self, status_callback=None):
        if not self.running or self.stopping:
            return
        self._thread = threading.Thread(target=self._stop_seq, args=(status_callback,), daemon=True)
        self._thread.start()

    def _start_seq(self, cb):
        self.starting = True
        def notify(msg):
            log.info(f"[Engine] {msg}")
            if cb: cb(msg)
        try:
            notify("Extending choke...")
            relay_set(RELAY_CHOKE_EXTEND, True)
            time.sleep(self.CHOKE_EXTEND_TIME)
            relay_set(RELAY_CHOKE_EXTEND, False)

            notify("Cranking...")
            relay_set(RELAY_STARTER, True)
            time.sleep(self.CRANK_TIME)
            relay_set(RELAY_STARTER, False)

            notify("Warming up at idle...")
            time.sleep(self.IDLE_WARM_TIME)

            notify("Retracting choke...")
            relay_set(RELAY_CHOKE_RETRACT, True)
            time.sleep(self.CHOKE_RETRACT_TIME)
            relay_set(RELAY_CHOKE_RETRACT, False)

            self.running = True
            notify("Engine RUNNING")
        except Exception as e:
            notify(f"Start FAILED: {e}")
            relay_set(RELAY_CHOKE_EXTEND,  False)
            relay_set(RELAY_CHOKE_RETRACT, False)
            relay_set(RELAY_STARTER,       False)
        finally:
            self.starting = False

    def _stop_seq(self, cb):
        self.stopping = True
        def notify(msg):
            log.info(f"[Engine] {msg}")
            if cb: cb(msg)
        try:
            notify("Closing all valves...")
            for rnum in [RELAY_VALVE_ZONE1, RELAY_VALVE_ZONE2,
                         RELAY_VALVE_ZONE3, RELAY_VALVE_ZONE4]:
                relay_set(rnum, False)

            notify("Killing ignition...")
            relay_set(RELAY_IGNITION_KILL, True)
            time.sleep(self.IGNITION_KILL_TIME)
            relay_set(RELAY_IGNITION_KILL, False)

            self.running = False
            notify("Engine STOPPED")
        except Exception as e:
            notify(f"Stop FAILED: {e}")
            relay_set(RELAY_IGNITION_KILL, False)
        finally:
            self.stopping = False

engine = EngineSequencer()

# ─── Frigate ───────────────────────────────────────────────────────────────────

def get_frigate_stats():
    try:
        import urllib.request
        with urllib.request.urlopen(
                f"{FRIGATE_URL}/api/events?limit=5&label=fire&label=smoke", timeout=2) as r:
            data = json.loads(r.read())
            return [f"{e.get('camera','?')} — {e.get('label','?')} {int(e.get('score',0)*100)}%"
                    for e in data]
    except Exception:
        return []

# ─── Colour palette ────────────────────────────────────────────────────────────

C = {
    "bg":        "#0d1117",
    "surface":   "#161b22",
    "border":    "#30363d",
    "text":      "#e6edf3",
    "muted":     "#8b949e",
    "green":     "#3fb950",
    "red":       "#f85149",
    "amber":     "#d29922",
    "blue":      "#58a6ff",
    "header_bg": "#21262d",
    "btn_off":   "#21262d",
    "btn_on":    "#1a3a1a",
    "btn_stop":  "#3a1a1a",
}

# Font sizes tuned for 800x480 touchscreen
FONT_HDR   = ("Courier New", 13, "bold")   # Header bar
FONT_TAB   = ("Courier New", 12, "bold")   # Tab labels
FONT_TITLE = ("Courier New", 14, "bold")   # Section titles
FONT_BIG   = ("Courier New", 22, "bold")   # Status indicators
FONT_BTN   = ("Courier New", 14, "bold")   # Buttons
FONT_BODY  = ("Courier New", 12)           # Body text
FONT_SMALL = ("Courier New", 11)           # Small labels / log

# ─── Main Application ──────────────────────────────────────────────────────────

class WildfirePanel(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Wildfire Defense System")
        self.configure(bg=C["bg"])

        if FULLSCREEN:
            self.attributes("-fullscreen", True)
        else:
            self.geometry(f"{DISPLAY_WIDTH}x{DISPLAY_HEIGHT}{SCREEN_OFFSET}")
            self.resizable(False, False)

        self.config(cursor="none")

        self.mode_auto    = tk.BooleanVar(value=False)
        self.valve_states = {i: tk.BooleanVar(value=False) for i in range(1, 5)}
        self.log_buffer   = deque(maxlen=LOG_BUFFER_SIZE)
        self.current_tab  = tk.StringVar(value="main")
        self.alert_count  = tk.IntVar(value=0)

        self._build_ui()
        self._start_polling()

        self.bind("<Escape>", lambda e: None)
        self.protocol("WM_DELETE_WINDOW", self._safe_quit)
        self._append_log("Panel started")

    # ─── UI construction ────────────────────────────────────────────────────────

    def _build_ui(self):
        # ── Header bar (40px) ──────────────────────────────────────────────────
        hdr = tk.Frame(self, bg=C["header_bg"], height=40)
        hdr.pack(fill="x", side="top")
        hdr.pack_propagate(False)

        tk.Label(hdr, text="◈ WILDFIRE DEFENSE",
                 font=FONT_HDR, bg=C["header_bg"], fg=C["amber"]).pack(side="left", padx=10)

        tk.Button(hdr, text="✕ QUIT", font=FONT_TAB,
                  bg=C["btn_stop"], fg=C["red"],
                  activebackground="#5a1a1a", relief="flat", bd=0,
                  padx=10, pady=0,
                  command=self._safe_quit).pack(side="right", padx=6, pady=4)

        self._lbl_time = tk.Label(hdr, text="", font=FONT_BODY,
                                  bg=C["header_bg"], fg=C["muted"])
        self._lbl_time.pack(side="right", padx=10)

        self._lbl_mode = tk.Label(hdr, text="MANUAL", font=FONT_TAB,
                                  bg=C["header_bg"], fg=C["muted"])
        self._lbl_mode.pack(side="right", padx=6)

        # ── Tab bar (44px) ─────────────────────────────────────────────────────
        tabbar = tk.Frame(self, bg=C["bg"], height=44)
        tabbar.pack(fill="x", side="top")
        tabbar.pack_propagate(False)

        self._tab_btns = {}
        tabs = [("main", "CONTROL"), ("zones", "ZONES"),
                ("camera", "CAMERA"), ("logs", "LOGS"), ("sys", "SYS")]
        for key, label in tabs:
            btn = tk.Button(tabbar, text=label, font=FONT_TAB,
                            bg=C["surface"], fg=C["muted"],
                            activebackground=C["border"], activeforeground=C["text"],
                            relief="flat", bd=0, padx=0, pady=8,
                            width=7,
                            command=lambda k=key: self._switch_tab(k))
            btn.pack(side="left", fill="y", expand=True)
            self._tab_btns[key] = btn

        # ── Content area (remaining height = 480-40-44 = 396px) ───────────────
        self._content = tk.Frame(self, bg=C["bg"])
        self._content.pack(fill="both", expand=True)

        self._pages = {}
        for key, _ in tabs:
            frame = tk.Frame(self._content, bg=C["bg"])
            self._pages[key] = frame

        self._build_main_tab()
        self._build_zones_tab()
        self._build_camera_tab()
        self._build_logs_tab()
        self._build_sys_tab()

        self._switch_tab("main")

    def _switch_tab(self, key):
        for k, frame in self._pages.items():
            frame.place_forget()
        self._pages[key].place(x=0, y=0, relwidth=1, relheight=1)
        self.current_tab.set(key)
        for k, btn in self._tab_btns.items():
            btn.configure(
                bg=C["header_bg"] if k == key else C["surface"],
                fg=C["text"] if k == key else C["muted"])

    # ─── CONTROL tab (800x396) ──────────────────────────────────────────────────
    # Layout:
    #   Row 1 (y=0..180):  Engine block (w=310) | Zones quick-view (w=480)
    #   Row 2 (y=184..260): Sequence status label + mode toggle
    #   Row 3 (y=264..396): Recent log
    #   Bottom (y=350..396): ALL OFF button

    def _build_main_tab(self):
        p = self._pages["main"]

        # ── Engine block ──────────────────────────────────────────────────────
        eng = tk.LabelFrame(p, text=" ENGINE ", font=FONT_SMALL,
                            bg=C["bg"], fg=C["muted"], bd=1, relief="solid")
        eng.place(x=4, y=4, width=310, height=178)

        self._lbl_engine_status = tk.Label(eng, text="● STOPPED",
                                           font=FONT_BIG, bg=C["bg"], fg=C["red"])
        self._lbl_engine_status.place(x=0, y=8, width=308, height=44)

        self._btn_engine_start = tk.Button(eng, text="▶  START ENGINE",
                                           font=FONT_BTN, bg=C["btn_on"], fg=C["green"],
                                           activebackground="#2a4a2a", relief="flat", bd=0,
                                           command=self._engine_start)
        self._btn_engine_start.place(x=8, y=60, width=292, height=48)

        self._btn_engine_stop = tk.Button(eng, text="■  STOP ENGINE",
                                          font=FONT_BTN, bg=C["btn_stop"], fg=C["red"],
                                          activebackground="#5a1a1a", relief="flat", bd=0,
                                          command=self._engine_stop)
        self._btn_engine_stop.place(x=8, y=116, width=292, height=48)

        # ── Mode toggle ───────────────────────────────────────────────────────
        mode = tk.LabelFrame(p, text=" MODE ", font=FONT_SMALL,
                             bg=C["bg"], fg=C["muted"], bd=1, relief="solid")
        mode.place(x=322, y=4, width=190, height=88)

        self._lbl_mode_big = tk.Label(mode, text="MANUAL",
                                      font=FONT_BIG, bg=C["bg"], fg=C["muted"])
        self._lbl_mode_big.place(x=0, y=4, width=188, height=40)

        self._btn_mode = tk.Button(mode, text="TOGGLE MODE",
                                   font=FONT_BTN, bg=C["btn_off"], fg=C["amber"],
                                   activebackground=C["border"], relief="flat", bd=0,
                                   command=self._mode_toggle)
        self._btn_mode.place(x=8, y=48, width=172, height=32)

        # ── Zone quick-view ───────────────────────────────────────────────────
        zq = tk.LabelFrame(p, text=" ZONES ", font=FONT_SMALL,
                           bg=C["bg"], fg=C["muted"], bd=1, relief="solid")
        zq.place(x=322, y=98, width=190, height=84)

        self._main_zone_labels = {}
        for i in range(1, 5):
            row = tk.Frame(zq, bg=C["bg"])
            row.place(x=4, y=(i-1)*18+4, width=182, height=18)
            lbl = tk.Label(row, text=f"Zone {i}: CLOSED",
                           font=FONT_SMALL, bg=C["bg"], fg=C["red"], anchor="w")
            lbl.pack(fill="x")
            self._main_zone_labels[i] = lbl

        # ── Sequence status ───────────────────────────────────────────────────
        self._lbl_seq = tk.Label(p, text="", font=FONT_SMALL,
                                 bg=C["bg"], fg=C["amber"], anchor="w")
        self._lbl_seq.place(x=4, y=188, width=790, height=20)

        # ── Recent log ────────────────────────────────────────────────────────
        log_frame = tk.LabelFrame(p, text=" RECENT EVENTS ", font=FONT_SMALL,
                                  bg=C["bg"], fg=C["muted"], bd=1, relief="solid")
        log_frame.place(x=4, y=212, width=510, height=130)

        self._mini_log = tk.Text(log_frame, font=FONT_SMALL,
                                 bg=C["surface"], fg=C["text"],
                                 relief="flat", bd=0, state="disabled",
                                 wrap="word", cursor="none")
        self._mini_log.pack(fill="both", expand=True, padx=2, pady=2)

        # ── ALL OFF button ────────────────────────────────────────────────────
        self._btn_all_off = tk.Button(p, text="⚡  ALL OFF",
                                      font=("Courier New", 18, "bold"),
                                      bg=C["btn_stop"], fg=C["red"],
                                      activebackground="#5a1a1a", relief="flat", bd=0,
                                      command=self._emergency_all_off)
        self._btn_all_off.place(x=522, y=212, width=274, height=130)

    # ─── ZONES tab ──────────────────────────────────────────────────────────────
    # 4 large zone buttons across the top, open-all/close-all below

    def _build_zones_tab(self):
        p = self._pages["zones"]
        self._zone_btns   = {}
        self._zone_labels = {}

        btn_w = 190
        for i in range(1, 5):
            x = (i-1) * 200 + 4
            frame = tk.LabelFrame(p, text=f" ZONE {i} ", font=FONT_SMALL,
                                  bg=C["bg"], fg=C["muted"], bd=1, relief="solid")
            frame.place(x=x, y=4, width=btn_w, height=200)

            lbl = tk.Label(frame, text="● CLOSED",
                           font=FONT_BIG, bg=C["bg"], fg=C["red"])
            lbl.place(x=0, y=10, width=btn_w-4, height=50)

            btn = tk.Button(frame, text="OPEN",
                            font=FONT_BTN, bg=C["btn_on"], fg=C["green"],
                            activebackground="#2a4a2a", relief="flat", bd=0,
                            command=lambda z=i: self._valve_toggle(z))
            btn.place(x=8, y=70, width=btn_w-20, height=110)

            self._zone_labels[i] = lbl
            self._zone_btns[i]   = btn

        # Open all / close all
        tk.Button(p, text="OPEN ALL ZONES", font=FONT_BTN,
                  bg=C["btn_on"], fg=C["green"],
                  activebackground="#2a4a2a", relief="flat", bd=0,
                  command=self._zones_all_open).place(x=4, y=212, width=390, height=60)

        tk.Button(p, text="CLOSE ALL ZONES", font=FONT_BTN,
                  bg=C["btn_stop"], fg=C["red"],
                  activebackground="#5a1a1a", relief="flat", bd=0,
                  command=self._zones_all_close).place(x=402, y=212, width=394, height=60)

        # Relay status table
        tbl = tk.LabelFrame(p, text=" RELAY STATUS ", font=FONT_SMALL,
                            bg=C["bg"], fg=C["muted"], bd=1, relief="solid")
        tbl.place(x=4, y=282, width=792, height=110)

        cols = ["RELAY", "FUNCTION", "STATE"]
        widths = [60, 260, 80]
        for ci, (h, w) in enumerate(zip(cols, widths)):
            tk.Label(tbl, text=h, font=FONT_SMALL,
                     bg=C["surface"], fg=C["muted"],
                     width=w//8).grid(row=0, column=ci, padx=2, pady=2, sticky="ew")

        relay_info = [
            (RELAY_CHOKE_EXTEND,  "Choke extend"),
            (RELAY_STARTER,       "Starter motor"),
            (RELAY_CHOKE_RETRACT, "Choke retract"),
            (RELAY_IGNITION_KILL, "Ignition kill"),
        ]
        self._relay_state_labels = {}
        for ri, (rnum, fname) in enumerate(relay_info):
            tk.Label(tbl, text=str(rnum), font=FONT_SMALL,
                     bg=C["bg"], fg=C["muted"]).grid(row=ri+1, column=0, padx=4, sticky="w")
            tk.Label(tbl, text=fname, font=FONT_SMALL,
                     bg=C["bg"], fg=C["text"]).grid(row=ri+1, column=1, padx=4, sticky="w")
            lbl = tk.Label(tbl, text="OFF", font=FONT_SMALL,
                           bg=C["bg"], fg=C["red"])
            lbl.grid(row=ri+1, column=2, padx=4, sticky="w")
            self._relay_state_labels[rnum] = lbl

    # ─── CAMERA tab ─────────────────────────────────────────────────────────────

    def _build_camera_tab(self):
        p = self._pages["camera"]

        tk.Label(p, text="FRIGATE DETECTIONS", font=FONT_TITLE,
                 bg=C["bg"], fg=C["amber"]).pack(pady=(12, 4))

        self._camera_alert_frame = tk.Frame(p, bg=C["bg"])
        self._camera_alert_frame.pack(fill="both", expand=True, padx=12)

        self._lbl_no_alerts = tk.Label(self._camera_alert_frame,
                                       text="No fire/smoke detections",
                                       font=FONT_BIG, bg=C["bg"], fg=C["muted"])
        self._lbl_no_alerts.pack(pady=30)

        cam_frame = tk.LabelFrame(p, text=" CAMERAS ", font=FONT_SMALL,
                                  bg=C["bg"], fg=C["muted"], bd=1, relief="solid")
        cam_frame.pack(fill="x", padx=12, pady=6)

        self._cam_labels = {}
        for i in range(1, 5):
            row = tk.Frame(cam_frame, bg=C["bg"])
            row.pack(fill="x", padx=6, pady=2)
            tk.Label(row, text=f"Camera {i}:", font=FONT_BODY,
                     bg=C["bg"], fg=C["muted"], width=12, anchor="w").pack(side="left")
            lbl = tk.Label(row, text="unknown", font=FONT_BODY,
                           bg=C["bg"], fg=C["muted"], anchor="w")
            lbl.pack(side="left")
            self._cam_labels[i] = lbl

        tk.Button(p, text="REFRESH", font=FONT_BTN,
                  bg=C["btn_off"], fg=C["blue"],
                  activebackground=C["border"], relief="flat", bd=0,
                  padx=20, pady=6,
                  command=self._poll_frigate).pack(pady=8)

    # ─── LOGS tab ───────────────────────────────────────────────────────────────

    def _build_logs_tab(self):
        p = self._pages["logs"]

        ctrl = tk.Frame(p, bg=C["bg"])
        ctrl.pack(fill="x", padx=6, pady=4)

        tk.Label(ctrl, text="SYSTEM LOG", font=FONT_TITLE,
                 bg=C["bg"], fg=C["text"]).pack(side="left")

        tk.Button(ctrl, text="CLEAR", font=FONT_BTN,
                  bg=C["btn_off"], fg=C["amber"],
                  activebackground=C["border"], relief="flat", bd=0,
                  padx=16, pady=4,
                  command=self._clear_log).pack(side="right")

        self._log_text = tk.Text(p, font=FONT_SMALL,
                                 bg=C["surface"], fg=C["text"],
                                 relief="flat", bd=0, state="disabled",
                                 wrap="word", cursor="none")
        self._log_text.pack(fill="both", expand=True, padx=6, pady=4)

    # ─── SYS tab ────────────────────────────────────────────────────────────────

    def _build_sys_tab(self):
        p = self._pages["sys"]

        tk.Label(p, text="SYSTEM", font=FONT_TITLE,
                 bg=C["bg"], fg=C["text"]).pack(pady=8)

        info = tk.LabelFrame(p, text=" INFO ", font=FONT_SMALL,
                             bg=C["bg"], fg=C["muted"], bd=1, relief="solid")
        info.pack(fill="x", padx=12, pady=4)

        self._lbl_cpu    = tk.Label(info, text="CPU: --",     font=FONT_BODY, bg=C["bg"], fg=C["text"], anchor="w")
        self._lbl_temp   = tk.Label(info, text="Temp: --",    font=FONT_BODY, bg=C["bg"], fg=C["text"], anchor="w")
        self._lbl_mem    = tk.Label(info, text="Memory: --",  font=FONT_BODY, bg=C["bg"], fg=C["text"], anchor="w")
        self._lbl_uptime = tk.Label(info, text="Uptime: --",  font=FONT_BODY, bg=C["bg"], fg=C["text"], anchor="w")
        self._lbl_hat    = tk.Label(info,
                                    text=f"Relay HAT: {'CONNECTED' if HAT_AVAILABLE else 'SIMULATION'}",
                                    font=FONT_BODY, bg=C["bg"],
                                    fg=C["green"] if HAT_AVAILABLE else C["amber"], anchor="w")
        for lbl in [self._lbl_cpu, self._lbl_temp, self._lbl_mem,
                    self._lbl_uptime, self._lbl_hat]:
            lbl.pack(fill="x", padx=10, pady=3)

        btn_frame = tk.Frame(p, bg=C["bg"])
        btn_frame.pack(pady=16)

        tk.Button(btn_frame, text="REBOOT Pi", font=FONT_BTN,
                  bg=C["btn_off"], fg=C["amber"],
                  activebackground=C["border"], relief="flat", bd=0,
                  padx=20, pady=10,
                  command=self._reboot).pack(side="left", padx=10)

        tk.Button(btn_frame, text="SHUTDOWN Pi", font=FONT_BTN,
                  bg=C["btn_stop"], fg=C["red"],
                  activebackground="#5a1a1a", relief="flat", bd=0,
                  padx=20, pady=10,
                  command=self._shutdown).pack(side="left", padx=10)

        tk.Button(btn_frame, text="QUIT APP", font=FONT_BTN,
                  bg=C["btn_off"], fg=C["muted"],
                  activebackground=C["border"], relief="flat", bd=0,
                  padx=20, pady=10,
                  command=self._safe_quit).pack(side="left", padx=10)

    # ─── Control actions ────────────────────────────────────────────────────────

    def _engine_start(self):
        if engine.running or engine.starting:
            return
        self._btn_engine_start.configure(state="disabled")
        self._append_log("Engine start sequence initiated")
        engine.start(status_callback=lambda msg: self.after(0, self._seq_status, msg))

    def _engine_stop(self):
        if not engine.running or engine.stopping:
            return
        self._btn_engine_stop.configure(state="disabled")
        self._append_log("Engine stop sequence initiated")
        engine.stop(status_callback=lambda msg: self.after(0, self._seq_status, msg))

    def _seq_status(self, msg):
        self._lbl_seq.configure(text=msg)
        self._append_log(f"[SEQ] {msg}")

    def _valve_toggle(self, zone_num):
        relay_num = [RELAY_VALVE_ZONE1, RELAY_VALVE_ZONE2,
                     RELAY_VALVE_ZONE3, RELAY_VALVE_ZONE4][zone_num - 1]
        new_state = not relay_get(relay_num)
        relay_set(relay_num, new_state)
        self.valve_states[zone_num].set(new_state)
        self._append_log(f"Zone {zone_num} {'OPEN' if new_state else 'CLOSED'}")
        self._update_zone_display(zone_num)

    def _zones_all_open(self):
        for i, rnum in enumerate([RELAY_VALVE_ZONE1, RELAY_VALVE_ZONE2,
                                   RELAY_VALVE_ZONE3, RELAY_VALVE_ZONE4], 1):
            relay_set(rnum, True)
            self.valve_states[i].set(True)
            self._update_zone_display(i)
        self._append_log("All zones OPENED")

    def _zones_all_close(self):
        for i, rnum in enumerate([RELAY_VALVE_ZONE1, RELAY_VALVE_ZONE2,
                                   RELAY_VALVE_ZONE3, RELAY_VALVE_ZONE4], 1):
            relay_set(rnum, False)
            self.valve_states[i].set(False)
            self._update_zone_display(i)
        self._append_log("All zones CLOSED")

    def _mode_toggle(self):
        new = not self.mode_auto.get()
        self.mode_auto.set(new)
        label = "AUTO" if new else "MANUAL"
        self._lbl_mode.configure(text=label, fg=C["green"] if new else C["muted"])
        self._lbl_mode_big.configure(text=label, fg=C["green"] if new else C["muted"])
        self._append_log(f"Mode → {label}")

    def _emergency_all_off(self):
        self._append_log("⚡ EMERGENCY ALL OFF triggered")
        for i, rnum in enumerate([RELAY_VALVE_ZONE1, RELAY_VALVE_ZONE2,
                                   RELAY_VALVE_ZONE3, RELAY_VALVE_ZONE4], 1):
            relay_set(rnum, False)
            self.valve_states[i].set(False)
            self._update_zone_display(i)
        engine.stop()

    # ─── Display helpers ────────────────────────────────────────────────────────

    def _update_zone_display(self, zone_num):
        on = self.valve_states[zone_num].get()
        text   = "● OPEN"   if on else "● CLOSED"
        color  = C["green"] if on else C["red"]
        btn_tx = "CLOSE"    if on else "OPEN"
        btn_bg = C["btn_stop"] if on else C["btn_on"]
        btn_fg = C["red"]      if on else C["green"]

        if zone_num in self._zone_labels:
            self._zone_labels[zone_num].configure(text=text, fg=color)
        if zone_num in self._zone_btns:
            self._zone_btns[zone_num].configure(text=btn_tx, bg=btn_bg, fg=btn_fg)
        if zone_num in self._main_zone_labels:
            self._main_zone_labels[zone_num].configure(
                text=f"Zone {zone_num}: {'OPEN' if on else 'CLOSED'}", fg=color)

    def _update_engine_display(self):
        if engine.running:
            self._lbl_engine_status.configure(text="● RUNNING", fg=C["green"])
            self._btn_engine_start.configure(state="disabled")
            self._btn_engine_stop.configure(state="normal")
        elif engine.starting:
            self._lbl_engine_status.configure(text="● STARTING", fg=C["amber"])
            self._btn_engine_start.configure(state="disabled")
            self._btn_engine_stop.configure(state="disabled")
        elif engine.stopping:
            self._lbl_engine_status.configure(text="● STOPPING", fg=C["amber"])
            self._btn_engine_start.configure(state="disabled")
            self._btn_engine_stop.configure(state="disabled")
        else:
            self._lbl_engine_status.configure(text="● STOPPED", fg=C["red"])
            self._btn_engine_start.configure(state="normal")
            self._btn_engine_stop.configure(state="normal")

    def _update_relay_table(self):
        for rnum, lbl in self._relay_state_labels.items():
            on = relay_get(rnum)
            lbl.configure(text="ON" if on else "OFF",
                          fg=C["green"] if on else C["red"])

    # ─── Polling ────────────────────────────────────────────────────────────────

    def _start_polling(self):
        self._tick()

    def _tick(self):
        try:
            now = datetime.now()
            self._lbl_time.configure(text=now.strftime("%H:%M:%S"))
            self._update_engine_display()
            self._update_relay_table()
            if now.second % 5 == 0:
                self._update_sysinfo()
            if now.second % 10 == 0:
                threading.Thread(target=self._poll_frigate, daemon=True).start()
        except Exception as e:
            log.error(f"Tick error: {e}")
        finally:
            self.after(1000, self._tick)

    def _update_sysinfo(self):
        try:
            cpu = subprocess.check_output(
                "top -bn1 | grep 'Cpu(s)' | awk '{print $2+$4}'",
                shell=True, text=True).strip()
            self._lbl_cpu.configure(text=f"CPU: {cpu}%")
        except Exception:
            pass
        try:
            with open("/sys/class/thermal/thermal_zone0/temp") as f:
                temp_c = int(f.read()) / 1000
            self._lbl_temp.configure(
                text=f"Temp: {temp_c:.1f}°C",
                fg=C["red"] if temp_c > 75 else C["text"])
        except Exception:
            pass
        try:
            mem = subprocess.check_output(
                "free -m | awk 'NR==2{printf \"%s/%sMB\", $3,$2}'",
                shell=True, text=True).strip()
            self._lbl_mem.configure(text=f"Memory: {mem}")
        except Exception:
            pass
        try:
            uptime = subprocess.check_output("uptime -p", shell=True, text=True).strip()
            self._lbl_uptime.configure(text=f"Uptime: {uptime}")
        except Exception:
            pass

    def _poll_frigate(self):
        alerts = get_frigate_stats()
        self.after(0, self._update_frigate_display, alerts)

    def _update_frigate_display(self, alerts):
        for w in self._camera_alert_frame.winfo_children():
            w.destroy()
        if not alerts:
            tk.Label(self._camera_alert_frame,
                     text="No fire/smoke detections",
                     font=FONT_BIG, bg=C["bg"], fg=C["muted"]).pack(pady=30)
            self.alert_count.set(0)
        else:
            self.alert_count.set(len(alerts))
            for a in alerts:
                tk.Label(self._camera_alert_frame,
                         text=f"⚠  {a}", font=FONT_BODY,
                         bg=C["surface"], fg=C["amber"],
                         anchor="w", relief="flat").pack(fill="x", pady=2, padx=4)

    # ─── Log ────────────────────────────────────────────────────────────────────

    def _append_log(self, msg: str):
        ts   = datetime.now().strftime("%H:%M:%S")
        line = f"[{ts}] {msg}\n"
        self.log_buffer.append(line)
        log.info(msg)
        for widget in [self._log_text, self._mini_log]:
            try:
                widget.configure(state="normal")
                widget.insert("end", line)
                widget.see("end")
                widget.configure(state="disabled")
            except Exception:
                pass

    def _clear_log(self):
        self.log_buffer.clear()
        self._log_text.configure(state="normal")
        self._log_text.delete("1.0", "end")
        self._log_text.configure(state="disabled")

    # ─── System ─────────────────────────────────────────────────────────────────

    def _reboot(self):
        if messagebox.askyesno("Reboot", "Reboot the Pi?"):
            self._append_log("Rebooting...")
            self._emergency_all_off()
            time.sleep(1)
            os.system("sudo reboot")

    def _shutdown(self):
        if messagebox.askyesno("Shutdown", "Shutdown the Pi?"):
            self._append_log("Shutting down...")
            self._emergency_all_off()
            time.sleep(1)
            os.system("sudo shutdown -h now")

    def _safe_quit(self):
        self._emergency_all_off()
        self.destroy()


# ─── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    app = WildfirePanel()
    app.mainloop()
