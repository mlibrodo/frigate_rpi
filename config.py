"""
config.py — Pyregon Control Panel Configuration Manager
Handles all tunable parameters with JSON persistence.
"""

import json
import os
import logging
from copy import deepcopy

logger = logging.getLogger(__name__)

CONFIG_PATH = os.path.join(os.path.dirname(__file__), "pyregon_config.json")

# ── Default values ──────────────────────────────────────────────────────────
DEFAULTS = {
    # AUTO-MODE TRIGGER
    "ember_trigger_confidence": 90,        # % confidence to begin watching
    "ember_trigger_duration": 30,          # seconds sustained above threshold → activate
    "ember_clear_confidence": 80,          # % below which we start counting down to stop
    "ember_clear_duration": 1800,          # seconds sustained below clear threshold → auto-stop (30 min)

    # ENGINE
    "engine_start_retries": 10,            # max retry attempts on failed start
    "engine_start_retry_delay": 15,        # seconds between retries

    # ZONE SEQUENCING — Phase 1
    "initial_upwind_duration": 300,        # seconds to run upwind zone before cycling (default 5 min)

    # ZONE SEQUENCING — Phase 2 cycling base durations (seconds per zone per cycle)
    "zone_base_duration": 120,             # base seconds per zone in clockwise rotation

    # WIND SPEED THRESHOLDS (mph)
    "wind_speed_moderate": 20,             # below this → equal durations
    "wind_speed_high": 40,                 # at or above this → 2x multiplier

    # ZONE DURATION MULTIPLIERS
    "duration_multiplier_moderate": 1.5,   # applied to upwind + adjacent zones at moderate wind
    "duration_multiplier_high": 2.0,       # applied to upwind + adjacent zones at high wind

    # ANEMOMETER FALLBACK
    "anemometer_poll_interval": 60,        # seconds between anemometer recovery checks
    "camera_fallback_window": 30,          # seconds rolling window for camera confidence average

    # ZONE GPS COORDINATES (set during installation via mobile app)
    # Each entry: {"zone_id": 1, "label": "Zone 1", "lat": 0.0, "lon": 0.0}
    "zones": [
        {"zone_id": 1, "label": "Zone 1", "lat": None, "lon": None},
        {"zone_id": 2, "label": "Zone 2", "lat": None, "lon": None},
        {"zone_id": 3, "label": "Zone 3", "lat": None, "lon": None},
        {"zone_id": 4, "label": "Zone 4", "lat": None, "lon": None},
    ],

    # CAMERA BEARINGS (fixed at install — Camera 1=N, 2=E, 3=S, 4=W)
    "cameras": [
        {"camera_id": 1, "label": "Camera 1", "bearing": 0,   "cardinal": "N"},
        {"camera_id": 2, "label": "Camera 2", "bearing": 90,  "cardinal": "E"},
        {"camera_id": 3, "label": "Camera 3", "bearing": 180, "cardinal": "S"},
        {"camera_id": 4, "label": "Camera 4", "bearing": 270, "cardinal": "W"},
    ],
}

# Settings exposed in the UI, grouped by section
# Format: (key, label, unit, input_type, min, max, step)
SETTINGS_SCHEMA = {
    "Detection Thresholds": [
        ("ember_trigger_confidence",  "Ember Trigger Confidence",   "%",   "int",   50, 100, 1),
        ("ember_trigger_duration",    "Sustained Detection to Arm", "sec", "int",   5,  300, 5),
        ("ember_clear_confidence",    "Ember Clear Confidence",     "%",   "int",   10, 90,  1),
        ("ember_clear_duration",      "Sustained Clear to Disarm",  "min", "int_m", 1,  120, 1),  # displayed as minutes
    ],
    "Engine": [
        ("engine_start_retries",      "Max Start Retries",          "",    "int",   1,  20,  1),
        ("engine_start_retry_delay",  "Delay Between Retries",      "sec", "int",   5,  60,  5),
    ],
    "Zone Sequencing": [
        ("initial_upwind_duration",   "Initial Upwind Soak",        "min", "int_m", 1,  60,  1),
        ("zone_base_duration",        "Base Zone Duration",         "sec", "int",   30, 600, 30),
    ],
    "Wind Speed Thresholds": [
        ("wind_speed_moderate",       "Moderate Wind Threshold",    "mph", "int",   5,  39,  1),
        ("wind_speed_high",           "High Wind Threshold",        "mph", "int",   20, 100, 1),
        ("duration_multiplier_moderate", "Moderate Wind Multiplier","x",  "float", 1.0, 3.0, 0.1),
        ("duration_multiplier_high",  "High Wind Multiplier",       "x",  "float", 1.0, 5.0, 0.1),
    ],
    "Sensor Fallback": [
        ("anemometer_poll_interval",  "Anemometer Recovery Poll",   "sec", "int",   10, 300, 10),
        ("camera_fallback_window",    "Camera Confidence Window",   "sec", "int",   10, 120, 5),
    ],
}


class Config:
    """Singleton config manager. Load once, write-through on every change."""

    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._data = {}
            cls._instance._loaded = False
        return cls._instance

    def load(self):
        """Load config from disk, filling missing keys with defaults."""
        if os.path.exists(CONFIG_PATH):
            try:
                with open(CONFIG_PATH, "r") as f:
                    saved = json.load(f)
                self._data = deepcopy(DEFAULTS)
                self._data.update(saved)
                logger.info(f"Config loaded from {CONFIG_PATH}")
            except Exception as e:
                logger.error(f"Failed to load config: {e}. Using defaults.")
                self._data = deepcopy(DEFAULTS)
        else:
            self._data = deepcopy(DEFAULTS)
            self.save()
            logger.info("No config found. Created defaults.")
        self._loaded = True

    def save(self):
        """Persist current config to disk."""
        try:
            with open(CONFIG_PATH, "w") as f:
                json.dump(self._data, f, indent=2)
            logger.info("Config saved.")
        except Exception as e:
            logger.error(f"Failed to save config: {e}")

    def get(self, key, default=None):
        if not self._loaded:
            self.load()
        return self._data.get(key, DEFAULTS.get(key, default))

    def set(self, key, value):
        """Set a value and immediately persist to disk."""
        if not self._loaded:
            self.load()
        self._data[key] = value
        self.save()

    def get_all(self):
        if not self._loaded:
            self.load()
        return deepcopy(self._data)

    def reset_to_defaults(self):
        self._data = deepcopy(DEFAULTS)
        self.save()
        logger.info("Config reset to defaults.")


# Module-level singleton
config = Config()
config.load()
