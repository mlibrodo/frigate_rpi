"""
auto_mode.py — Pyregon AUTO Mode State Machine
Handles ember detection → engine start → zone sequencing → auto-stop.

Detection feed: pump.py (Roboflow) pushes ember confidence into
AutoModeController.feed_detection() — no external client needed.
"""

import threading
import time
import math
import logging
from collections import deque
from enum import Enum, auto
from config import config

logger = logging.getLogger(__name__)


# ── Enums ────────────────────────────────────────────────────────────────────

class AutoState(Enum):
    IDLE          = auto()   # Waiting — AUTO not triggered
    WATCHING      = auto()   # Ember detected, counting down to activation
    STARTING      = auto()   # Engine start sequence in progress
    PHASE1        = auto()   # Initial upwind zone soak
    CYCLING       = auto()   # Full clockwise zone cycling
    CLEARING      = auto()   # Threat below threshold, counting down to stop
    STOPPING      = auto()   # Shutdown sequence in progress


# ── Wind / Zone Utilities ────────────────────────────────────────────────────

def bearing_between(lat1, lon1, lat2, lon2):
    """Calculate compass bearing from point 1 to point 2 (degrees, 0=N)."""
    lat1, lon1, lat2, lon2 = map(math.radians, [lat1, lon1, lat2, lon2])
    dlon = lon2 - lon1
    x = math.sin(dlon) * math.cos(lat2)
    y = math.cos(lat1) * math.sin(lat2) - math.sin(lat1) * math.cos(lat2) * math.cos(dlon)
    bearing = math.degrees(math.atan2(x, y))
    return (bearing + 360) % 360


def zone_bearing(zone, property_center):
    """Return bearing from property center to a zone's GPS coordinate."""
    if zone["lat"] is None or zone["lon"] is None:
        return None
    return bearing_between(
        property_center["lat"], property_center["lon"],
        zone["lat"], zone["lon"]
    )


def angular_diff(a, b):
    """Smallest angular difference between two bearings (0–180)."""
    diff = abs(a - b) % 360
    return diff if diff <= 180 else 360 - diff


def order_zones_from_wind(wind_from_bearing, zones, property_center):
    """
    Return zone IDs ordered: upwind first, then clockwise through adjacent,
    downwind last. Wind FROM bearing means fire approaches FROM that direction,
    so the face closest to that bearing is upwind and gets water first.

    Returns: list of zone_ids in activation order
    """
    # Compute bearing from center to each zone
    zone_bearings = []
    for z in zones:
        b = zone_bearing(z, property_center)
        if b is not None:
            zone_bearings.append((z["zone_id"], b))

    if not zone_bearings:
        # Fallback: return zones in ID order
        return [z["zone_id"] for z in zones]

    # Sort zones by angular distance from the wind_from_bearing (upwind first)
    zone_bearings.sort(key=lambda zb: angular_diff(zb[1], wind_from_bearing))
    upwind_id, upwind_bearing = zone_bearings[0]

    # Remaining zones ordered clockwise from upwind
    remaining = zone_bearings[1:]
    remaining.sort(key=lambda zb: (zb[1] - upwind_bearing) % 360)

    return [upwind_id] + [z[0] for z in remaining]


def compute_zone_duration(zone_index, num_zones, wind_speed, base_duration,
                          mod_moderate, mod_high, thresh_moderate, thresh_high):
    """
    Returns runtime (seconds) for a zone given its position in the ordered list.
    Index 0 = upwind, index num_zones-1 = downwind.
    Adjacent = everything between upwind and downwind.
    Downwind = last zone.
    """
    is_downwind = (zone_index == num_zones - 1)

    if wind_speed < thresh_moderate:
        multiplier = 1.0
    elif wind_speed < thresh_high:
        multiplier = 1.0 if is_downwind else mod_moderate
    else:
        multiplier = 1.0 if is_downwind else mod_high

    return int(base_duration * multiplier)


# ── Camera Confidence Tracker ────────────────────────────────────────────────

class CameraConfidenceTracker:
    """
    Maintains a rolling window of per-camera ember confidence readings.
    Used as anemometer fallback to determine upwind bearing.

    Camera bearing map (fixed at install):
      Camera 1 → 0°  (N)
      Camera 2 → 90° (E)
      Camera 3 → 180° (S)
      Camera 4 → 270° (W)
    """

    CAMERA_BEARINGS = {1: 0, 2: 90, 3: 180, 4: 270}

    def __init__(self):
        window = config.get("camera_fallback_window")
        self._readings = {cam_id: deque() for cam_id in self.CAMERA_BEARINGS}
        self._window = window

    def update_window(self):
        self._window = config.get("camera_fallback_window")

    def record(self, camera_id, confidence, timestamp=None):
        ts = timestamp or time.time()
        if camera_id in self._readings:
            self._readings[camera_id].append((ts, confidence))
            self._prune(camera_id, ts)

    def _prune(self, camera_id, now):
        cutoff = now - self._window
        q = self._readings[camera_id]
        while q and q[0][0] < cutoff:
            q.popleft()

    def best_upwind_bearing(self):
        """Return bearing of camera with highest sustained average confidence."""
        now = time.time()
        best_cam, best_avg = None, -1
        for cam_id, readings in self._readings.items():
            self._prune(cam_id, now)
            if not readings:
                continue
            avg = sum(c for _, c in readings) / len(readings)
            if avg > best_avg:
                best_avg = avg
                best_cam = cam_id
        if best_cam is None:
            return None
        return self.CAMERA_BEARINGS[best_cam]


# ── Alert Interface ──────────────────────────────────────────────────────────

class AlertInterface:
    """Stub alert dispatcher. Replace with real SMS/push/sound implementation."""

    def send(self, message, level="INFO"):
        logger.log(logging.WARNING if level != "INFO" else logging.INFO,
                   f"[ALERT/{level}] {message}")


# ── AUTO Mode Controller ─────────────────────────────────────────────────────

class AutoModeController:
    """
    Core AUTO mode state machine.

    External interfaces required (inject or subclass):
      - relay_controller: object with start_engine(), stop_engine(),
                          open_zone(zone_id), close_zone(zone_id), close_all_zones()
      - anemometer:       object with get_wind_speed() → float (mph),
                          get_wind_direction() → float (degrees, 0=N),
                          is_online() → bool

    Detection feed:
      Call feed_detection(camera_id, label, confidence) directly from
      pump.py / Roboflow output. No external MQTT client needed.
    """

    DETECTION_TTL = 5.0   # seconds before a detection expires

    def __init__(self, relay_controller, anemometer,
                 property_center=None, on_state_change=None):
        self.relay = relay_controller
        self.anemometer = anemometer
        self.property_center = property_center or {"lat": 38.933, "lon": -119.984}
        self.on_state_change = on_state_change  # callback(AutoState)

        # Internal detection store: list of [timestamp, camera_id, label, confidence]
        self._detections      = []
        self._detection_lock  = threading.Lock()
        self._last_confidence = 0.0   # most recent ember confidence (0–100)

        self.camera_tracker = CameraConfidenceTracker()
        self.alerter = AlertInterface()

        self._state = AutoState.IDLE
        self._enabled = False
        self._thread = None
        self._stop_event = threading.Event()

        # Runtime tracking
        self._watch_start = None
        self._clear_start = None
        self._current_zone = None
        self._ordered_zones = []
        self._cycle_index = 0
        self._engine_running = False
        self._anemometer_was_online = True

    # ── Public API ────────────────────────────────────────────────────────────

    def enable(self):
        """Enable AUTO mode — starts the monitoring loop."""
        if self._enabled:
            return
        self._enabled = True
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        logger.info("AUTO mode enabled.")

    def disable(self):
        """Disable AUTO mode and stop all operations."""
        self._enabled = False
        self._stop_event.set()
        if self._engine_running:
            self._execute_stop_sequence()
        self._set_state(AutoState.IDLE)
        logger.info("AUTO mode disabled.")

    def is_enabled(self):
        return self._enabled

    def get_state(self):
        return self._state

    def feed_detection(self, camera_id: int, label: str, confidence: float):
        """
        Push a detection from pump.py / Roboflow into the AUTO mode engine.
        Call this from _read_pump_output() in control_panel.py.

        confidence: 0.0–1.0 (raw Roboflow score)
        """
        label = label.lower()
        now = time.time()
        with self._detection_lock:
            # Remove stale entries for same camera+label, add fresh one
            self._detections = [
                d for d in self._detections
                if not (d[1] == camera_id and d[2] == label)
                and (now - d[0]) < self.DETECTION_TTL
            ]
            self._detections.append([now, camera_id, label, confidence])
            self._last_confidence = confidence * 100.0

        # Also feed camera confidence tracker for anemometer fallback
        if label in ("ember", "fire", "smoke"):
            self.camera_tracker.record(camera_id, confidence)

    def get_max_ember_confidence(self) -> float:
        """Returns highest live ember confidence (0–100%). Used by UI."""
        now = time.time()
        with self._detection_lock:
            live = [d[3] for d in self._detections
                    if (now - d[0]) < self.DETECTION_TTL
                    and d[2] in ("ember", "fire", "smoke")]
        return max(live) * 100.0 if live else 0.0

    # ── Internal Loop ─────────────────────────────────────────────────────────

    def _run(self):
        logger.info("AUTO mode monitoring loop started.")
        while not self._stop_event.is_set():
            try:
                self._tick()
            except Exception as e:
                logger.error(f"AUTO mode error in tick: {e}", exc_info=True)
            time.sleep(1)
        logger.info("AUTO mode monitoring loop stopped.")

    def _tick(self):
        ember_confidence = self.get_max_ember_confidence()

        # Check anemometer health
        self._check_anemometer_health()

        if self._state == AutoState.IDLE:
            if ember_confidence >= config.get("ember_trigger_confidence"):
                self._watch_start = time.time()
                self._set_state(AutoState.WATCHING)
                logger.info(f"Ember detected at {ember_confidence:.1f}% — watching.")

        elif self._state == AutoState.WATCHING:
            if ember_confidence >= config.get("ember_trigger_confidence"):
                elapsed = time.time() - self._watch_start
                if elapsed >= config.get("ember_trigger_duration"):
                    logger.info("Sustained ember detection — initiating AUTO activation.")
                    self._set_state(AutoState.STARTING)
                    threading.Thread(target=self._execute_start_sequence, daemon=True).start()
            else:
                logger.info("Ember confidence dropped — returning to IDLE.")
                self._watch_start = None
                self._set_state(AutoState.IDLE)

        elif self._state in (AutoState.PHASE1, AutoState.CYCLING):
            if ember_confidence < config.get("ember_clear_confidence"):
                if self._clear_start is None:
                    self._clear_start = time.time()
                    logger.info(f"Ember confidence below clear threshold ({ember_confidence:.1f}%) — starting clear countdown.")
                elif time.time() - self._clear_start >= config.get("ember_clear_duration"):
                    logger.info("Sustained clear condition — initiating auto-stop.")
                    self._set_state(AutoState.STOPPING)
                    threading.Thread(target=self._execute_stop_sequence, daemon=True).start()
            else:
                if self._clear_start is not None:
                    logger.info("Ember confidence recovered — reset clear countdown.")
                self._clear_start = None

        elif self._state == AutoState.CLEARING:
            pass  # Handled in stop sequence thread

    # ── Anemometer Health ──────────────────────────────────────────────────────

    def _check_anemometer_health(self):
        online = self.anemometer.is_online()
        if not online and self._anemometer_was_online:
            logger.warning("Anemometer went offline — switching to camera fallback.")
            self.alerter.send("Anemometer offline. Using camera fallback for wind direction.", level="WARNING")
        elif online and not self._anemometer_was_online:
            logger.info("Anemometer recovered — resuming normal wind readings.")
            self.alerter.send("Anemometer back online.", level="INFO")
        self._anemometer_was_online = online

    def _get_wind_direction(self):
        """Return wind FROM bearing. Falls back to camera tracker if anemometer offline."""
        if self.anemometer.is_online():
            return self.anemometer.get_wind_direction()
        logger.warning("Using camera confidence as wind direction proxy.")
        bearing = self.camera_tracker.best_upwind_bearing()
        if bearing is None:
            logger.warning("No camera data available — defaulting wind direction to North.")
            return 0
        return bearing

    def _get_wind_speed(self):
        if self.anemometer.is_online():
            return self.anemometer.get_wind_speed()
        return 0.0  # Conservative — no multiplier if speed unknown

    # ── Start Sequence ─────────────────────────────────────────────────────────

    def _execute_start_sequence(self):
        retries = config.get("engine_start_retries")
        delay = config.get("engine_start_retry_delay")

        for attempt in range(1, retries + 1):
            logger.info(f"Engine start attempt {attempt}/{retries}...")
            success = self.relay.start_engine()
            if success:
                self._engine_running = True
                logger.info("Engine started successfully.")
                self._begin_phase1()
                return
            logger.warning(f"Engine start attempt {attempt} failed.")
            if attempt < retries:
                time.sleep(delay)

        # All retries exhausted
        msg = f"ENGINE FAILED TO START after {retries} attempts. Manual intervention required."
        logger.error(msg)
        self.alerter.send(msg, level="CRITICAL")
        self._set_state(AutoState.IDLE)

    # ── Phase 1: Initial Upwind Soak ───────────────────────────────────────────

    def _begin_phase1(self):
        self._set_state(AutoState.PHASE1)
        self._clear_start = None

        wind_bearing = self._get_wind_direction()
        zones = config.get("zones")
        self._ordered_zones = order_zones_from_wind(wind_bearing, zones, self.property_center)

        upwind_zone_id = self._ordered_zones[0]
        duration = config.get("initial_upwind_duration")

        logger.info(f"Phase 1: Opening upwind zone {upwind_zone_id} for {duration}s "
                    f"(wind from {wind_bearing:.0f}°).")
        self.relay.open_zone(upwind_zone_id)
        self._current_zone = upwind_zone_id

        time.sleep(duration)

        if self._state == AutoState.PHASE1:
            self.relay.close_zone(upwind_zone_id)
            self._begin_cycling()

    # ── Phase 2: Clockwise Cycling ─────────────────────────────────────────────

    def _begin_cycling(self):
        self._set_state(AutoState.CYCLING)
        self._cycle_index = 0

        while self._state == AutoState.CYCLING and not self._stop_event.is_set():
            # Recalculate zone order at start of each zone (not mid-zone)
            wind_bearing = self._get_wind_direction()
            wind_speed = self._get_wind_speed()
            zones = config.get("zones")
            self._ordered_zones = order_zones_from_wind(wind_bearing, zones, self.property_center)

            num_zones = len(self._ordered_zones)
            zone_id = self._ordered_zones[self._cycle_index % num_zones]

            duration = compute_zone_duration(
                zone_index=self._cycle_index % num_zones,
                num_zones=num_zones,
                wind_speed=wind_speed,
                base_duration=config.get("zone_base_duration"),
                mod_moderate=config.get("duration_multiplier_moderate"),
                mod_high=config.get("duration_multiplier_high"),
                thresh_moderate=config.get("wind_speed_moderate"),
                thresh_high=config.get("wind_speed_high"),
            )

            logger.info(f"Cycling: Zone {zone_id} (position {self._cycle_index % num_zones + 1}/{num_zones}), "
                        f"{duration}s | wind {wind_speed:.0f}mph from {wind_bearing:.0f}°")

            self.relay.open_zone(zone_id)
            self._current_zone = zone_id
            time.sleep(duration)
            self.relay.close_zone(zone_id)
            self._current_zone = None

            self._cycle_index += 1

    # ── Stop Sequence ──────────────────────────────────────────────────────────

    def _execute_stop_sequence(self):
        self._set_state(AutoState.STOPPING)
        logger.info("AUTO stop sequence initiated.")
        self.relay.close_all_zones()
        time.sleep(2)
        self.relay.stop_engine()
        self._engine_running = False
        self._clear_start = None
        self._cycle_index = 0
        self._current_zone = None
        self._set_state(AutoState.IDLE)
        logger.info("AUTO stop sequence complete.")

    # ── State Management ───────────────────────────────────────────────────────

    def _set_state(self, new_state):
        if self._state != new_state:
            logger.info(f"AUTO state: {self._state.name} → {new_state.name}")
            self._state = new_state
            if self.on_state_change:
                try:
                    self.on_state_change(new_state)
                except Exception as e:
                    logger.error(f"on_state_change callback error: {e}")
