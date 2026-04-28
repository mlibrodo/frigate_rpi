#!/usr/bin/env python3
"""
Pyregon Wildfire Defense - Control Panel
Optimized for 800x480 5" touchscreen on Raspberry Pi

Integrated modules:
  - config.py          : persistent JSON settings
  - auto_mode.py       : AUTO mode state machine
  - anemometer.py      : Renke RS-CFSFX-N01 Modbus RTU driver
  - settings_modal.py  : gear-icon settings overlay

Detection pipeline:
  pump.py → Roboflow API → JSON stdout → _read_pump_output()
  → AutoModeController.feed_detection() → AUTO mode state machine
"""

import tkinter as tk
from tkinter import messagebox
import threading
import time
import json
import os
import subprocess
import logging
import io
import atexit
import signal
from datetime import datetime
from collections import deque

import web_panel as _web_panel_mod

try:
    from PIL import Image, ImageTk
    PIL_AVAILABLE = True
except ImportError:
    PIL_AVAILABLE = False

# ── Pyregon modules ────────────────────────────────────────────────────────────
from config import config
from auto_mode import AutoModeController, AutoState
from anemometer import Anemometer, SimulatedAnemometer
from settings_modal import SettingsModal
from sensors import HATSensors

# ─── Configuration ─────────────────────────────────────────────────────────────

DISPLAY_WIDTH  = 800
DISPLAY_HEIGHT = 480
FULLSCREEN     = False
SCREEN_OFFSET  = "+3840+0"

# Relay mapping (Sequent Microsystems 8-relay HAT, board=0)
RELAY_CHOKE_EXTEND   = 1
RELAY_STARTER        = 2
RELAY_CHOKE_RETRACT  = 3
RELAY_IGNITION_KILL  = 4
RELAY_VALVE_ZONE1    = 5
RELAY_VALVE_ZONE2    = 6
RELAY_VALVE_ZONE3    = 7
RELAY_VALVE_ZONE4    = 8

ZONE_RELAYS = {1: RELAY_VALVE_ZONE1, 2: RELAY_VALVE_ZONE2,
               3: RELAY_VALVE_ZONE3, 4: RELAY_VALVE_ZONE4}

# Frigate / pump
FRIGATE_URL   = "http://localhost:5000"

# Camera snapshot config
CAMERAS = [
    ("tahoe_cam1", "CAM 1 - NORTH"),
    ("tahoe_cam2", "CAM 2 - EAST"),
    ("tahoe_cam3", "CAM 3 - SOUTH"),
    ("tahoe_cam4", "CAM 4 - WEST"),
]
SNAPSHOT_REFRESH_SEC = 30

def fetch_camera_snapshot(cam_name):
    """Fetch latest JPEG from Frigate for a camera. Returns bytes or None."""
    try:
        import urllib.request
        url = f"{FRIGATE_URL}/api/{cam_name}/latest.jpg?h=110"
        with urllib.request.urlopen(url, timeout=4) as r:
            return r.read()
    except Exception:
        return None
PUMP_SCRIPT   = "/home/librodo112/frigate_rpi/pump.py"
PUMP_CAMERA   = "tahoe_cam1"
PUMP_API_KEY  = "B5p60bLPJYURpEpoGHcc"
PUMP_MODEL_ID = "ember-training-poc/1"
PUMP_FPS      = "2.0"

# Anemometer serial port
ANEMOMETER_PORT    = "/dev/ttyUSB0"
ANEMOMETER_ADDRESS = 1
ANEMOMETER_BAUD    = 4800

# Log file
LOG_FILE        = "/var/log/wildfire_panel.log"
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

# ─── Relay controller bridge ───────────────────────────────────────────────────
# Adapts existing relay_set() to the interface AutoModeController expects.

class RelayControllerBridge:
    """
    Translates AutoModeController calls into relay_set() calls
    using the existing EngineSequencer for start/stop sequences.
    """
    def __init__(self, engine_sequencer, status_callback=None):
        self._engine = engine_sequencer
        self._cb     = status_callback   # set by WildfirePanel after init

    def set_status_callback(self, cb):
        self._cb = cb

    def start_engine(self) -> bool:
        """Runs the existing choke+crank sequence. Returns True if engine started."""
        if self._engine.running:
            return True
        event = threading.Event()
        result = [False]

        def _cb(msg):
            if self._cb:
                self._cb(msg)
            if "RUNNING" in msg:
                result[0] = True
                event.set()
            elif "FAILED" in msg:
                event.set()

        self._engine.start(status_callback=_cb)
        event.wait(timeout=30)   # generous timeout for full choke+crank sequence
        return result[0]

    def stop_engine(self):
        if self._engine.running:
            self._engine.stop(status_callback=self._cb)

    def open_zone(self, zone_id: int):
        rnum = ZONE_RELAYS.get(zone_id)
        if rnum:
            relay_set(rnum, True)
            log.info(f"[AUTO] Zone {zone_id} OPEN")

    def close_zone(self, zone_id: int):
        rnum = ZONE_RELAYS.get(zone_id)
        if rnum:
            relay_set(rnum, False)
            log.info(f"[AUTO] Zone {zone_id} CLOSED")

    def close_all_zones(self):
        for zone_id, rnum in ZONE_RELAYS.items():
            relay_set(rnum, False)
        log.info("[AUTO] All zones CLOSED")

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
        self._thread = threading.Thread(
            target=self._start_seq, args=(status_callback,), daemon=True)
        self._thread.start()

    def stop(self, status_callback=None):
        if not self.running or self.stopping:
            return
        self._thread = threading.Thread(
            target=self._stop_seq, args=(status_callback,), daemon=True)
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
            for rnum in ZONE_RELAYS.values():
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

FONT_HDR   = ("Courier New", 13, "bold")
FONT_TAB   = ("Courier New", 12, "bold")
FONT_TITLE = ("Courier New", 14, "bold")
FONT_BIG   = ("Courier New", 22, "bold")
FONT_BTN   = ("Courier New", 14, "bold")
FONT_BODY  = ("Courier New", 12)
FONT_SMALL = ("Courier New", 11)

# AUTO state → display colour mapping
AUTO_STATE_COLOR = {
    AutoState.IDLE:     C["muted"],
    AutoState.WATCHING: C["amber"],
    AutoState.STARTING: C["amber"],
    AutoState.PHASE1:   C["blue"],
    AutoState.CYCLING:  C["green"],
    AutoState.CLEARING: C["amber"],
    AutoState.STOPPING: C["red"],
}

# ─── Main Application ──────────────────────────────────────────────────────────

class WildfirePanel(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Pyregon Wildfire Defense")
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
        self._pump_proc   = None
        self._pump_top    = "unknown"
        self._pump_conf   = 0.0
        self._is_auto_mode = False   # thread-safe mirror of mode_auto BooleanVar

        # ── Sensor / AUTO mode initialisation ─────────────────────────────────
        self._init_sensors()
        self._build_ui()
        self._start_polling()
        self._start_pump()
        # Fetch snapshots immediately on launch
        threading.Thread(target=self._refresh_snapshots, daemon=True).start()

        # ── Web panel (FastAPI + WebSocket, daemon thread) ─────────────────────
        self._web = _web_panel_mod.WebPanel(
            get_state    = self._web_state,
            engine_start = lambda: self.after(0, self._engine_start),
            engine_stop  = lambda: self.after(0, self._engine_stop),
            zone_toggle  = lambda z: self.after(0, self._valve_toggle, z),
            mode_toggle  = lambda: self.after(0, self._mode_toggle),
            all_off      = lambda: self.after(0, self._emergency_all_off),
        )
        self._web.start()

        self.bind("<Escape>", lambda e: None)
        self.protocol("WM_DELETE_WINDOW", self._safe_quit)
        self._append_log("Panel started")

        # Start in AUTO mode
        self.after(500, self._mode_toggle)

    # ─── Sensor / AUTO mode init ───────────────────────────────────────────────

    def _init_sensors(self):
        """Initialise anemometer, relay bridge, and AUTO controller."""

        # Anemometer — fall back to simulation if port unavailable
        try:
            self._anemometer = Anemometer(
                port=ANEMOMETER_PORT,
                device_address=ANEMOMETER_ADDRESS,
                baud_rate=ANEMOMETER_BAUD,
                poll_interval=1.0,
            )
            if not self._anemometer.connect():
                raise RuntimeError("Could not open serial port")
            self._anemometer.start_polling()
            log.info("Anemometer connected.")
        except Exception as e:
            log.warning(f"Anemometer unavailable ({e}) — using simulation.")
            self._anemometer = SimulatedAnemometer(speed_mph=12.0, direction_deg=45.0)

        # SM-1-029 HAT sensors (temp, pressure, battery, throttle relays)
        self._hat_sensors = HATSensors()
        self._hat_sensors.start()

        # Relay bridge
        self._relay_bridge = RelayControllerBridge(engine)

        # AUTO mode controller — detection fed directly from pump.py output
        property_center = config.get("property_center") or {"lat": 38.933, "lon": -119.984}
        self._auto_ctrl = AutoModeController(
            relay_controller = self._relay_bridge,
            anemometer       = self._anemometer,
            property_center  = property_center,
            on_state_change  = self._on_auto_state_change,
        )
        # Wire status callback into relay bridge
        self._relay_bridge.set_status_callback(
            lambda msg: self.after(0, self._seq_status, msg)
        )

        # Track Roboflow pump status for UI
        self._roboflow_online = False

    # ─── UI construction ────────────────────────────────────────────────────────

    def _build_ui(self):
        # ── Header bar (40px) ──────────────────────────────────────────────────
        hdr = tk.Frame(self, bg=C["header_bg"], height=40)
        hdr.pack(fill="x", side="top")
        hdr.pack_propagate(False)

        tk.Label(hdr, text="◈ PYREGON WILDFIRE DEFENSE",
                 font=FONT_HDR, bg=C["header_bg"], fg=C["amber"]).pack(side="left", padx=10)

        tk.Button(hdr, text="✕ QUIT", font=FONT_TAB,
                  bg=C["btn_stop"], fg=C["red"],
                  activebackground="#5a1a1a", relief="flat", bd=0,
                  padx=10, pady=0,
                  command=self._safe_quit).pack(side="right", padx=6, pady=4)

        # Gear / settings button
        gear_btn, self._settings_modal = SettingsModal.create_gear_button(hdr, self)
        gear_btn.pack(side="right", padx=2, pady=4)

        self._lbl_time = tk.Label(hdr, text="", font=FONT_BODY,
                                  bg=C["header_bg"], fg=C["muted"])
        self._lbl_time.pack(side="right", padx=10)

        self._lbl_mode = tk.Label(hdr, text="MANUAL", font=FONT_TAB,
                                  bg=C["header_bg"], fg=C["muted"])
        self._lbl_mode.pack(side="right", padx=6)

        # Wind readout in header (speed + direction)
        self._lbl_wind = tk.Label(hdr, text="~ -- mph  --°",
                                  font=FONT_BODY, bg=C["header_bg"], fg=C["blue"])
        self._lbl_wind.pack(side="right", padx=10)

        # Anemometer online dot
        self._lbl_anemo_dot = tk.Label(hdr, text="●", font=FONT_BODY,
                                       bg=C["header_bg"], fg=C["muted"])
        self._lbl_anemo_dot.pack(side="right", padx=2)

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

        # ── Content area ───────────────────────────────────────────────────────
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
                fg=C["text"]      if k == key else C["muted"])

    # ─── CONTROL tab ───────────────────────────────────────────────────────────
    # Layout (800×396):
    #   Col A (x=4,   w=310): Engine block (h=178)
    #   Col B (x=322, w=190): Mode block (h=88) + Zone quick-view (h=84)
    #   Col C (x=520, w=276): AUTO status panel (h=178)
    #   Row 2 (y=188): Sequence status
    #   Row 3 (y=212): Recent log (w=510) | ALL OFF (w=274)

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

        # ── AUTO status panel ─────────────────────────────────────────────────
        auto_panel = tk.LabelFrame(p, text=" AUTO STATUS ", font=FONT_SMALL,
                                   bg=C["bg"], fg=C["muted"], bd=1, relief="solid")
        auto_panel.place(x=520, y=4, width=276, height=178)

        # State indicator
        self._lbl_auto_state = tk.Label(auto_panel, text="IDLE",
                                        font=FONT_BIG, bg=C["bg"], fg=C["muted"])
        self._lbl_auto_state.place(x=0, y=4, width=274, height=40)

        # Wind speed + direction
        self._lbl_wind_detail = tk.Label(auto_panel,
                                         text="Wind: -- mph  --°  (--)",
                                         font=FONT_SMALL, bg=C["bg"], fg=C["blue"])
        self._lbl_wind_detail.place(x=6, y=48, width=262, height=18)

        # Ember confidence bar background
        tk.Label(auto_panel, text="Ember:", font=FONT_SMALL,
                 bg=C["bg"], fg=C["muted"]).place(x=6, y=70, width=50, height=16)

        self._ember_bar_bg = tk.Frame(auto_panel, bg=C["surface"],
                                      width=190, height=12)
        self._ember_bar_bg.place(x=58, y=72)
        self._ember_bar_fill = tk.Frame(self._ember_bar_bg, bg=C["red"], height=12)
        self._ember_bar_fill.place(x=0, y=0, width=0, height=12)

        self._lbl_ember_pct = tk.Label(auto_panel, text="0%",
                                       font=FONT_SMALL, bg=C["bg"], fg=C["muted"])
        self._lbl_ember_pct.place(x=252, y=70, width=20, height=16)

        # Active zone indicator
        self._lbl_active_zone = tk.Label(auto_panel, text="Zone: --",
                                         font=FONT_SMALL, bg=C["bg"], fg=C["muted"])
        self._lbl_active_zone.place(x=6, y=92, width=262, height=18)

        # Anemometer status
        self._lbl_anemo_status = tk.Label(auto_panel, text="Anemometer: --",
                                          font=FONT_SMALL, bg=C["bg"], fg=C["muted"])
        self._lbl_anemo_status.place(x=6, y=114, width=262, height=18)

        # Roboflow / pump.py status
        self._lbl_roboflow_status = tk.Label(auto_panel, text="Roboflow: --",
                                             font=FONT_SMALL, bg=C["bg"], fg=C["muted"])
        self._lbl_roboflow_status.place(x=6, y=136, width=262, height=18)

        # Cycle counter
        self._lbl_cycle = tk.Label(auto_panel, text="Cycle: --",
                                   font=FONT_SMALL, bg=C["bg"], fg=C["muted"])
        self._lbl_cycle.place(x=6, y=156, width=262, height=16)

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

        tk.Button(p, text="OPEN ALL ZONES", font=FONT_BTN,
                  bg=C["btn_on"], fg=C["green"],
                  activebackground="#2a4a2a", relief="flat", bd=0,
                  command=self._zones_all_open).place(x=4, y=212, width=390, height=60)

        tk.Button(p, text="CLOSE ALL ZONES", font=FONT_BTN,
                  bg=C["btn_stop"], fg=C["red"],
                  activebackground="#5a1a1a", relief="flat", bd=0,
                  command=self._zones_all_close).place(x=402, y=212, width=394, height=60)

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

        # ── Header ────────────────────────────────────────────────────────────
        hdr = tk.Frame(p, bg=C["bg"])
        hdr.pack(fill="x", padx=8, pady=(6, 2))
        tk.Label(hdr, text="EMBER DETECTION", font=FONT_TITLE,
                 bg=C["bg"], fg=C["amber"]).pack(side="left")
        self._lbl_snap_time = tk.Label(hdr, text="", font=FONT_SMALL,
                                       bg=C["bg"], fg=C["muted"])
        self._lbl_snap_time.pack(side="left", padx=10)
        tk.Button(hdr, text="REFRESH CAMS", font=FONT_SMALL,
                  bg=C["btn_off"], fg=C["blue"],
                  activebackground=C["border"], relief="flat", bd=0,
                  padx=8, pady=2,
                  command=lambda: threading.Thread(
                      target=self._refresh_snapshots, daemon=True).start()
                  ).pack(side="right")

        # ── Roboflow / pump result ─────────────────────────────────────────────
        result_frame = tk.LabelFrame(p, text=" ROBOFLOW INFERENCE ", font=FONT_SMALL,
                                     bg=C["bg"], fg=C["muted"], bd=1, relief="solid")
        result_frame.pack(fill="x", padx=12, pady=(2, 4))

        self._lbl_pump_result = tk.Label(result_frame, text="?  Waiting...",
                                         font=FONT_BIG, bg=C["bg"], fg=C["muted"])
        self._lbl_pump_result.pack(pady=(6, 2))

        self._lbl_pump_conf = tk.Label(result_frame, text="Confidence: --",
                                       font=FONT_BODY, bg=C["bg"], fg=C["muted"])
        self._lbl_pump_conf.pack(pady=(0, 6))

        # ── Snapshot + confidence grid ─────────────────────────────────────────
        # 4 cells side by side, each with snapshot image on top, label + confidence below
        cam_frame = tk.LabelFrame(p, text=" LIVE SNAPSHOTS  |  EMBER CONFIDENCE ",
                                  font=FONT_SMALL,
                                  bg=C["bg"], fg=C["muted"], bd=1, relief="solid")
        cam_frame.pack(fill="x", padx=12, pady=2)

        cam_labels_row = tk.Frame(cam_frame, bg=C["bg"])
        cam_labels_row.pack(fill="x", padx=4, pady=4)

        self._cam_conf_labels  = {}
        self._cam_image_labels = {}
        self._cam_photoref     = {}

        IMG_W, IMG_H = 168, 110
        cam_info = [
            (1, "tahoe_cam1", "CAM 1 - NORTH"),
            (2, "tahoe_cam2", "CAM 2 - EAST"),
            (3, "tahoe_cam3", "CAM 3 - SOUTH"),
            (4, "tahoe_cam4", "CAM 4 - WEST"),
        ]
        for cam_id, cam_name, label in cam_info:
            cell = tk.Frame(cam_labels_row, bg=C["surface"], bd=1, relief="solid")
            cell.pack(side="left", expand=True, fill="x", padx=3)

            # Snapshot image
            img_lbl = tk.Label(cell, bg=C["surface"], text="—",
                               font=FONT_SMALL, fg=C["muted"],
                               width=IMG_W, height=IMG_H)
            img_lbl.pack(pady=(4, 2), padx=2)
            self._cam_image_labels[cam_name] = img_lbl

            # Camera label
            tk.Label(cell, text=label, font=FONT_SMALL,
                     bg=C["surface"], fg=C["muted"]).pack()

            # Confidence
            conf_lbl = tk.Label(cell, text="0%", font=FONT_BIG,
                                bg=C["surface"], fg=C["muted"])
            conf_lbl.pack(pady=(0, 4))
            self._cam_conf_labels[cam_id] = conf_lbl

        # Model info
        tk.Label(p, text=f"Model: {PUMP_MODEL_ID}  |  Camera: {PUMP_CAMERA}",
                 font=FONT_SMALL, bg=C["bg"], fg=C["muted"]).pack(pady=2)

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

        self._lbl_cpu    = tk.Label(info, text="CPU: --",    font=FONT_BODY, bg=C["bg"], fg=C["text"], anchor="w")
        self._lbl_temp   = tk.Label(info, text="Temp: --",   font=FONT_BODY, bg=C["bg"], fg=C["text"], anchor="w")
        self._lbl_mem    = tk.Label(info, text="Memory: --", font=FONT_BODY, bg=C["bg"], fg=C["text"], anchor="w")
        self._lbl_uptime = tk.Label(info, text="Uptime: --", font=FONT_BODY, bg=C["bg"], fg=C["text"], anchor="w")
        self._lbl_hat    = tk.Label(info,
                                    text=f"Relay HAT: {'CONNECTED' if HAT_AVAILABLE else 'SIMULATION'}",
                                    font=FONT_BODY, bg=C["bg"],
                                    fg=C["green"] if HAT_AVAILABLE else C["amber"], anchor="w")
        # Anemometer / Frigate status rows
        self._lbl_sys_anemo    = tk.Label(info, text="Anemometer: --",
                                          font=FONT_BODY, bg=C["bg"], fg=C["text"], anchor="w")
        self._lbl_sys_roboflow = tk.Label(info, text="Roboflow: --",
                                          font=FONT_BODY, bg=C["bg"], fg=C["text"], anchor="w")

        # SM-1-029 HAT sensor rows
        hat_status = "CONNECTED" if self._hat_sensors.is_online() else "NOT DETECTED"
        hat_color  = C["green"] if self._hat_sensors.is_online() else C["amber"]
        self._lbl_sensor_hat  = tk.Label(info, text=f"Sensor HAT: {hat_status}",
                                         font=FONT_BODY, bg=C["bg"], fg=hat_color, anchor="w")
        self._lbl_sensor_temp = tk.Label(info, text="Amb. Temp: --",
                                         font=FONT_BODY, bg=C["bg"], fg=C["text"], anchor="w")
        self._lbl_sensor_pres = tk.Label(info, text="Water Pressure: --",
                                         font=FONT_BODY, bg=C["bg"], fg=C["text"], anchor="w")
        self._lbl_sensor_batt = tk.Label(info, text="Battery: --",
                                         font=FONT_BODY, bg=C["bg"], fg=C["text"], anchor="w")

        for lbl in [self._lbl_cpu, self._lbl_temp, self._lbl_mem,
                    self._lbl_uptime, self._lbl_hat,
                    self._lbl_sys_anemo, self._lbl_sys_roboflow,
                    self._lbl_sensor_hat, self._lbl_sensor_temp,
                    self._lbl_sensor_pres, self._lbl_sensor_batt]:
            lbl.pack(fill="x", padx=10, pady=2)

        btn_frame = tk.Frame(p, bg=C["bg"])
        btn_frame.pack(pady=12)

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
        rnum = ZONE_RELAYS[zone_num]
        new_state = not relay_get(rnum)
        relay_set(rnum, new_state)
        self.valve_states[zone_num].set(new_state)
        self._append_log(f"Zone {zone_num} {'OPEN' if new_state else 'CLOSED'}")
        self._update_zone_display(zone_num)

    def _zones_all_open(self):
        for i, rnum in ZONE_RELAYS.items():
            relay_set(rnum, True)
            self.valve_states[i].set(True)
            self._update_zone_display(i)
        self._append_log("All zones OPENED")

    def _zones_all_close(self):
        for i, rnum in ZONE_RELAYS.items():
            relay_set(rnum, False)
            self.valve_states[i].set(False)
            self._update_zone_display(i)
        self._append_log("All zones CLOSED")

    def _mode_toggle(self):
        new = not self.mode_auto.get()
        self.mode_auto.set(new)
        self._is_auto_mode = new  # keep thread-safe mirror in sync
        label = "AUTO" if new else "MANUAL"
        self._lbl_mode.configure(text=label, fg=C["green"] if new else C["muted"])
        self._lbl_mode_big.configure(text=label, fg=C["green"] if new else C["muted"])
        self._append_log(f"Mode → {label}")

        if new:
            self._auto_ctrl.enable()
            self._append_log("AUTO mode controller ENABLED")
        else:
            self._auto_ctrl.disable()
            self._append_log("AUTO mode controller DISABLED")

    def _emergency_all_off(self):
        self._append_log("⚡ EMERGENCY ALL OFF triggered")
        # Disable AUTO mode first so it can't re-open valves
        if self.mode_auto.get():
            self.mode_auto.set(False)
            self._auto_ctrl.disable()
            self._lbl_mode.configure(text="MANUAL", fg=C["muted"])
            self._lbl_mode_big.configure(text="MANUAL", fg=C["muted"])
        for i, rnum in ZONE_RELAYS.items():
            relay_set(rnum, False)
            self.valve_states[i].set(False)
            self._update_zone_display(i)
        self._stop_pump()
        engine.stop()

    # ─── AUTO mode callbacks ───────────────────────────────────────────────────

    def _on_auto_state_change(self, new_state: AutoState):
        """Called by AutoModeController on every state transition (background thread)."""
        self.after(0, self._apply_auto_state_ui, new_state)

    def _apply_auto_state_ui(self, state: AutoState):
        """Update CONTROL tab AUTO status panel — must be called on main thread."""
        color = AUTO_STATE_COLOR.get(state, C["muted"])
        self._lbl_auto_state.configure(text=state.name, fg=color)
        self._append_log(f"[AUTO] State → {state.name}")

        # Sync valve display after AUTO opens/closes zones
        for i, rnum in ZONE_RELAYS.items():
            self.valve_states[i].set(relay_get(rnum))
            self._update_zone_display(i)

    def _on_ember_detection(self, camera_id: int, confidence: float):
        """Called from _read_pump_output when pump.py reports an ember detection."""
        conf_pct = confidence * 100
        self.after(0, self._update_camera_confidence, camera_id, conf_pct)

    def _update_camera_confidence(self, camera_id: int, conf_pct: float):
        """Update per-camera confidence label on CAMERA tab."""
        if camera_id not in self._cam_conf_labels:
            return
        lbl = self._cam_conf_labels[camera_id]
        if conf_pct >= 90:
            color = C["red"]
        elif conf_pct >= 70:
            color = C["amber"]
        elif conf_pct > 0:
            color = C["blue"]
        else:
            color = C["muted"]
        lbl.configure(text=f"{conf_pct:.0f}%", fg=color)

    # ─── Display helpers ────────────────────────────────────────────────────────

    def _update_zone_display(self, zone_num):
        on     = self.valve_states[zone_num].get()
        text   = "● OPEN"      if on else "● CLOSED"
        color  = C["green"]    if on else C["red"]
        btn_tx = "CLOSE"       if on else "OPEN"
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
        state = (engine.running, engine.starting, engine.stopping)
        if state == getattr(self, "_prev_engine_state", None):
            return
        self._prev_engine_state = state
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
        snapshot = {rnum: relay_get(rnum) for rnum in self._relay_state_labels}
        if snapshot == getattr(self, "_prev_relay_snapshot", None):
            return
        self._prev_relay_snapshot = snapshot
        for rnum, lbl in self._relay_state_labels.items():
            on = snapshot[rnum]
            lbl.configure(text="ON" if on else "OFF",
                          fg=C["green"] if on else C["red"])

    def _update_auto_panel(self):
        """Refresh the AUTO status panel on the CONTROL tab (called every 2 s)."""
        # Wind — only reconfigure labels when the displayed text changes
        reading = self._anemometer.get_reading() if hasattr(self._anemometer, 'get_reading') else None
        if reading:
            wind_detail = f"Wind: {reading.speed_mph:.1f} mph  {reading.direction_deg}°  ({reading.cardinal()})"
            wind_hdr    = f"~ {reading.speed_mph:.0f} mph  {reading.direction_deg}°"
            wind_fg     = C["blue"]
        else:
            wind_detail = "Wind: -- mph  --°  (--)"
            wind_hdr    = "~ -- mph  --°"
            wind_fg     = C["muted"]
        if wind_detail != getattr(self, "_prev_wind_detail", None):
            self._lbl_wind_detail.configure(text=wind_detail, fg=wind_fg)
            self._lbl_wind.configure(text=wind_hdr, fg=wind_fg)
            self._prev_wind_detail = wind_detail

        # Ember confidence bar
        ember_pct = self._auto_ctrl.get_max_ember_confidence()
        if ember_pct != getattr(self, "_prev_ember_pct", None):
            bar_w     = int(190 * min(ember_pct / 100.0, 1.0))
            bar_color = C["red"] if ember_pct >= 90 else (C["amber"] if ember_pct >= 70 else C["blue"])
            self._ember_bar_fill.place(x=0, y=0, width=bar_w, height=12)
            self._ember_bar_fill.configure(bg=bar_color if bar_w > 0 else C["surface"])
            self._lbl_ember_pct.configure(
                text=f"{ember_pct:.0f}%",
                fg=bar_color if ember_pct > 0 else C["muted"])
            self._prev_ember_pct = ember_pct

        # Active zone / cycle
        zone  = self._auto_ctrl._current_zone
        cycle = self._auto_ctrl._cycle_index
        zone_text  = f"Zone: {zone if zone else '--'}"
        cycle_text = f"Cycle step: {cycle}" if self.mode_auto.get() else "Cycle: --"
        if zone_text != getattr(self, "_prev_zone_text", None):
            self._lbl_active_zone.configure(text=zone_text, fg=C["green"] if zone else C["muted"])
            self._prev_zone_text = zone_text
        if cycle_text != getattr(self, "_prev_cycle_text", None):
            self._lbl_cycle.configure(text=cycle_text, fg=C["muted"])
            self._prev_cycle_text = cycle_text

        # Anemometer online status
        anemo_online = self._anemometer.is_online()
        if anemo_online != getattr(self, "_prev_anemo_online", None):
            self._lbl_anemo_dot.configure(fg=C["green"] if anemo_online else C["red"])
            self._lbl_anemo_status.configure(
                text=f"Anemometer: {'ONLINE' if anemo_online else 'OFFLINE — camera fallback'}",
                fg=C["green"] if anemo_online else C["amber"])
            self._lbl_sys_anemo.configure(
                text=f"Anemometer: {'ONLINE' if anemo_online else 'OFFLINE'}",
                fg=C["green"] if anemo_online else C["red"])
            self._prev_anemo_online = anemo_online

        # Roboflow / pump.py status
        if self._roboflow_online != getattr(self, "_prev_roboflow_online", None):
            self._lbl_roboflow_status.configure(
                text=f"Roboflow: {'RECEIVING' if self._roboflow_online else 'NO DATA'}",
                fg=C["green"] if self._roboflow_online else C["red"])
            self._lbl_sys_roboflow.configure(
                text=f"Roboflow: {'RECEIVING' if self._roboflow_online else 'NO DATA'}",
                fg=C["green"] if self._roboflow_online else C["red"])
            self._prev_roboflow_online = self._roboflow_online

        # Decay camera confidence labels when no live detections
        live_cams = {d[1] for d in self._auto_ctrl._detections
                     if (time.time() - d[0]) < self._auto_ctrl.DETECTION_TTL}
        for cam_id, lbl in self._cam_conf_labels.items():
            if cam_id not in live_cams:
                lbl.configure(text="0%", fg=C["muted"])

    # ─── Web panel state snapshot ────────────────────────────────────────────────

    def _web_state(self) -> dict:
        """Collect a JSON-serialisable state snapshot for the web dashboard."""
        reading = self._anemometer.get_reading() if hasattr(self._anemometer, 'get_reading') else None
        eng = engine
        return {
            "type":           "state",
            "ts":             datetime.now().strftime("%H:%M:%S"),
            "engine":         ("RUNNING"  if eng.running  else
                               "STARTING" if eng.starting else
                               "STOPPING" if eng.stopping else "STOPPED"),
            "mode":           "AUTO" if self._is_auto_mode else "MANUAL",
            "auto_state":     self._auto_ctrl.get_state().name,
            "active_zone":    self._auto_ctrl._current_zone,
            "zones":          {str(i): relay_get(ZONE_RELAYS[i]) for i in range(1, 5)},
            "wind_speed":     round(reading.speed_mph, 1) if reading else None,
            "wind_dir":       reading.direction_deg        if reading else None,
            "wind_card":      reading.cardinal()           if reading else None,
            "ember_pct":      round(self._auto_ctrl.get_max_ember_confidence(), 1),
            "anemo_online":   self._anemometer.is_online(),
            "roboflow_online": self._roboflow_online,
            "hat_online":     self._hat_sensors.is_online() if hasattr(self, '_hat_sensors') else False,
            "sensor_temp":    (self._hat_sensors.get_reading().temp_c        if hasattr(self, '_hat_sensors') and self._hat_sensors.get_reading().valid else None),
            "sensor_pressure":(self._hat_sensors.get_reading().pressure_psi  if hasattr(self, '_hat_sensors') and self._hat_sensors.get_reading().valid else None),
            "sensor_battery": (self._hat_sensors.get_reading().battery_v     if hasattr(self, '_hat_sensors') and self._hat_sensors.get_reading().valid else None),
        }

    # ─── Polling ────────────────────────────────────────────────────────────────

    def _start_polling(self):
        self._tick()

    def _tick(self):
        try:
            now = datetime.now()
            self._lbl_time.configure(text=now.strftime("%H:%M:%S"))
            self._update_engine_display()
            self._update_relay_table()
            if getattr(self, "_pump_display_dirty", False):
                self._pump_display_dirty = False
                self._update_pump_display()
            if now.second % 2 == 0:
                self._update_auto_panel()
                if hasattr(self, '_web'):
                    self._web.push_state()
            if now.second % 5 == 0:
                self._update_sysinfo()
            if now.second % 10 == 0:
                threading.Thread(target=self._poll_frigate, daemon=True).start()
            if now.second % SNAPSHOT_REFRESH_SEC == 0:
                threading.Thread(target=self._refresh_snapshots, daemon=True).start()
        except Exception as e:
            log.error(f"Tick error: {e}")
        finally:
            self.after(1000, self._tick)

    def _update_sysinfo(self):
        try:
            with open("/proc/stat") as f:
                fields = list(map(int, f.readline().split()[1:]))
            idle, total = fields[3], sum(fields)
            prev = getattr(self, "_cpu_prev", (idle, total))
            d_idle = idle - prev[0]; d_total = total - prev[1]
            cpu_pct = 100.0 * (1.0 - d_idle / d_total) if d_total else 0.0
            self._cpu_prev = (idle, total)
            self._lbl_cpu.configure(text=f"CPU: {cpu_pct:.1f}%")
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
            info = {}
            with open("/proc/meminfo") as f:
                for line in f:
                    k, v = line.split(":")
                    info[k.strip()] = int(v.split()[0])
            total_mb = info["MemTotal"] // 1024
            used_mb  = total_mb - info["MemAvailable"] // 1024
            self._lbl_mem.configure(text=f"Memory: {used_mb}/{total_mb}MB")
        except Exception:
            pass
        try:
            with open("/proc/uptime") as f:
                secs = int(float(f.read().split()[0]))
            h, m = divmod(secs // 60, 60)
            d, h = divmod(h, 24)
            parts = ([f"{d}d"] if d else []) + ([f"{h}h"] if h else []) + [f"{m}m"]
            self._lbl_uptime.configure(text="Uptime: up " + " ".join(parts))
        except Exception:
            pass
        if hasattr(self, '_hat_sensors'):
            r = self._hat_sensors.get_reading()
            if r.valid:
                self._lbl_sensor_temp.configure(text=f"Amb. Temp: {r.temp_c:.1f} °C")
                self._lbl_sensor_pres.configure(text=f"Water Pressure: {r.pressure_psi:.1f} PSI")
                self._lbl_sensor_batt.configure(text=f"Battery: {r.battery_v:.2f} V")
            else:
                status = "online (no data)" if self._hat_sensors.is_online() else "offline"
                self._lbl_sensor_hat.configure(text=f"Sensor HAT: {status}")

    # ─── Pump process ───────────────────────────────────────────────────────────

    def _start_pump(self):
        try:
            env = os.environ.copy()
            env["FRIGATE_CAMERAS"]  = PUMP_CAMERA
            env["ROBOFLOW_API_KEY"] = PUMP_API_KEY
            env["MODEL_ID"]         = PUMP_MODEL_ID
            env["PUMP_FPS"]         = PUMP_FPS
            env["FRIGATE_URL"]      = FRIGATE_URL
            self._pump_proc = subprocess.Popen(
                ["python3", "-u", PUMP_SCRIPT],
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                bufsize=1,
            )
            atexit.register(self._stop_pump)  # ensure cleanup on any exit
            threading.Thread(target=self._read_pump_output, daemon=True).start()
            self._append_log("Pump started (tahoe_cam1 → Roboflow)")
        except Exception as e:
            self._append_log(f"Pump start failed: {e}")

    def _stop_pump(self):
        if self._pump_proc and self._pump_proc.poll() is None:
            self._pump_proc.terminate()
            try:
                self._pump_proc.wait(timeout=3)
            except Exception:
                self._pump_proc.kill()
            self._append_log("Pump stopped")
        self._pump_proc = None

    def _read_pump_output(self):
        """
        Parse JSON lines from pump.py stdout.
        Feeds ember detections directly into AutoModeController.
        """
        self._pump_display_dirty = False
        self._last_pump_error_log = 0.0
        try:
            for line in self._pump_proc.stdout:
                line = line.strip()
                if not line:
                    continue
                try:
                    data = json.loads(line)
                except json.JSONDecodeError:
                    continue

                event = data.get("event")
                if event == "prediction":
                    self._pump_top  = data.get("top", "unknown")
                    self._pump_conf = float(data.get("confidence") or 0.0)
                    self._roboflow_online = True
                    self._pump_display_dirty = True  # flushed by _tick, not after(0)

                    # Feed ember detections into AUTO mode controller
                    if self._pump_top == "ember":
                        # Camera 1 = North (pump.py monitors tahoe_cam1)
                        self._auto_ctrl.feed_detection(
                            camera_id  = 1,
                            label      = "ember",
                            confidence = self._pump_conf,
                        )
                        self._on_ember_detection(1, self._pump_conf)

                elif event == "error":
                    self._roboflow_online = False
                    now = time.time()
                    if now - self._last_pump_error_log >= 10:
                        self._last_pump_error_log = now
                        msg = f"[PUMP] {data.get('error', 'unknown error')}"
                        self.after(0, self._append_log, msg)
        except Exception as e:
            self._roboflow_online = False
            self.after(0, self._append_log, f"Pump reader error: {e}")

    def _update_pump_display(self):
        top      = self._pump_top
        conf     = self._pump_conf
        conf_pct = f"{conf*100:.1f}%"
        if top == "ember":
            color = C["red"];   icon = "🔥 EMBER DETECTED"
        elif top == "no_ember":
            color = C["green"]; icon = "✓  No Ember"
        else:
            color = C["muted"]; icon = "?  Unknown"

        if hasattr(self, "_lbl_pump_result"):
            if (icon, color) != getattr(self, "_prev_pump_result", None):
                self._lbl_pump_result.configure(text=icon, fg=color)
                self._prev_pump_result = (icon, color)
        if hasattr(self, "_lbl_pump_conf"):
            if conf_pct != getattr(self, "_prev_pump_conf_pct", None):
                self._lbl_pump_conf.configure(text=f"Confidence: {conf_pct}")
                self._prev_pump_conf_pct = conf_pct

        if hasattr(self, "_cam_conf_labels"):
            conf_display = conf * 100
            if top == "ember":
                cam_color = C["red"] if conf_display >= 90 else (
                            C["amber"] if conf_display >= 70 else C["blue"])
                new_cam = (f"{conf_display:.0f}%", cam_color)
            else:
                new_cam = ("0%", C["muted"])
            if new_cam != getattr(self, "_prev_cam1_conf", None):
                self._cam_conf_labels[1].configure(text=new_cam[0], fg=new_cam[1])
                self._prev_cam1_conf = new_cam

    def _refresh_snapshots(self):
        """Fetch and decode snapshots in background; hand PIL Images to main thread."""
        IMG_W, IMG_H = 168, 110
        updates = []
        for cam_name, _ in CAMERAS:
            jpeg = fetch_camera_snapshot(cam_name)
            pil_img = None
            if jpeg and PIL_AVAILABLE:
                try:
                    pil_img = Image.open(io.BytesIO(jpeg)).resize(
                        (IMG_W, IMG_H), Image.LANCZOS)
                except Exception:
                    pass
            updates.append((cam_name, pil_img, bool(jpeg)))
        ts = datetime.now().strftime("%H:%M:%S")
        self.after(0, self._apply_snapshots, updates, IMG_W, IMG_H, ts)

    def _apply_snapshots(self, updates, w, h, ts):
        """Apply pre-decoded PIL images on the main thread (PhotoImage must be main-thread)."""
        for cam_name, pil_img, had_jpeg in updates:
            lbl = self._cam_image_labels.get(cam_name)
            if not lbl:
                continue
            if pil_img:
                try:
                    photo = ImageTk.PhotoImage(pil_img)
                    self._cam_photoref[cam_name] = photo
                    lbl.configure(image=photo, text="", width=w, height=h)
                except Exception:
                    lbl.configure(image="", text="Error", fg=C["red"])
            elif had_jpeg and not PIL_AVAILABLE:
                lbl.configure(image="", text="Install Pillow", fg=C["amber"])
            else:
                lbl.configure(image="", text="No feed", fg=C["muted"])
        self._lbl_snap_time.configure(text=f"Updated {ts}")

    def _poll_frigate(self):
        pass  # Detection driven entirely by pump.py → Roboflow output

    # ─── Log ────────────────────────────────────────────────────────────────────

    def _append_log(self, msg: str):
        ts   = datetime.now().strftime("%H:%M:%S")
        line = f"[{ts}] {msg}\n"
        self.log_buffer.append(line)
        log.info(msg)
        if hasattr(self, '_web'):
            self._web.push_event(msg)
        for widget in [self._log_text, self._mini_log]:
            try:
                widget.configure(state="normal")
                widget.insert("end", line)
                n = int(widget.index("end-1c").split(".")[0])
                if n > 120:
                    widget.delete("1.0", f"{n - 100}.0")
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
        if self.mode_auto.get():
            self._auto_ctrl.disable()
        self._anemometer.stop_polling()
        self._stop_pump()
        self._emergency_all_off()
        self.destroy()


# ─── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    app = WildfirePanel()
    app.mainloop()
