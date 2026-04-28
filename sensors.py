"""
sensors.py — SM-1-029 (Sequent 4rel4in HAT) sensor interface
Stack level 1, stacked above existing 8-relay HAT (level 0).

Sensor wiring — 4-20mA industrial transmitters:
  Input 1 → Ambient temperature
  Input 2 → Pump water pressure
  Input 3 → Battery voltage

Throttle actuator relays:
  Relay 1 → Extend  (throttle up)
  Relay 2 → Retract (throttle down)
  (only one should be energised at a time)

CALIBRATION: adjust the *_MIN / *_MAX constants below to match
your actual sensor datasheets (4 mA = min value, 20 mA = max value).
"""

import logging
import threading
from dataclasses import dataclass, field
from typing import Optional

log = logging.getLogger(__name__)

# ── Stack / wiring ────────────────────────────────────────────────────────────
STACK_LEVEL            = 1
THROTTLE_EXTEND_RELAY  = 1
THROTTLE_RETRACT_RELAY = 2

# ── Sensor ranges (4 mA = min, 20 mA = max) ──────────────────────────────────
TEMP_MIN_C      =   0.0   # adjust to match your temperature transmitter
TEMP_MAX_C      = 100.0
PRESSURE_MIN    =   0.0   # adjust to match your pressure transmitter (PSI)
PRESSURE_MAX    = 150.0
BATT_MIN_V      =   0.0   # adjust to match your voltage transmitter
BATT_MAX_V      =  15.0

POLL_INTERVAL   =   5.0   # seconds between reads


# ── Data class ────────────────────────────────────────────────────────────────

@dataclass
class SensorReading:
    temp_c:       Optional[float] = None
    pressure_psi: Optional[float] = None
    battery_v:    Optional[float] = None
    valid:        bool             = False


# ── Driver ────────────────────────────────────────────────────────────────────

class HATSensors:
    """
    Reads SM-1-029 HAT sensors on a background thread.
    Gracefully degrades to offline mode if the HAT is not present.
    """

    def __init__(self, stack: int = STACK_LEVEL):
        self._stack   = stack
        self._hat     = None
        self._reading = SensorReading()
        self._lock    = threading.Lock()
        self._online  = False
        self._stop    = threading.Event()

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def start(self):
        try:
            import sm_4rel4in
            self._hat    = sm_4rel4in.SM4rel4in(self._stack)
            self._online = True
            log.info(f"HATSensors: SM4rel4in detected at stack {self._stack}")
        except Exception as e:
            log.warning(f"HATSensors: HAT not found (stack={self._stack}) — {e}")
            return

        threading.Thread(target=self._loop, daemon=True, name="hat-sensors").start()

    def stop(self):
        self._stop.set()

    # ── Public API ────────────────────────────────────────────────────────────

    def is_online(self) -> bool:
        return self._online

    def get_reading(self) -> SensorReading:
        with self._lock:
            return self._reading

    def throttle_extend(self):
        """Energise extend relay, release retract."""
        self._set_relay(THROTTLE_EXTEND_RELAY,  True)
        self._set_relay(THROTTLE_RETRACT_RELAY, False)

    def throttle_retract(self):
        """Energise retract relay, release extend."""
        self._set_relay(THROTTLE_RETRACT_RELAY, True)
        self._set_relay(THROTTLE_EXTEND_RELAY,  False)

    def throttle_stop(self):
        """Release both throttle relays."""
        self._set_relay(THROTTLE_EXTEND_RELAY,  False)
        self._set_relay(THROTTLE_RETRACT_RELAY, False)

    # ── Internal ──────────────────────────────────────────────────────────────

    def _set_relay(self, relay: int, state: bool):
        if not self._hat:
            return
        try:
            self._hat.set_relay(relay, 1 if state else 0)
        except Exception as e:
            log.error(f"HATSensors relay {relay}: {e}")

    def _loop(self):
        while not self._stop.is_set():
            r = SensorReading()
            try:
                r.temp_c       = round(self._read_4_20ma(1, TEMP_MIN_C,   TEMP_MAX_C),   1)
                r.pressure_psi = round(self._read_4_20ma(2, PRESSURE_MIN,  PRESSURE_MAX), 1)
                r.battery_v    = round(self._read_4_20ma(3, BATT_MIN_V,    BATT_MAX_V),   2)
                r.valid        = True
            except Exception as e:
                log.error(f"HATSensors poll error: {e}")
            with self._lock:
                self._reading = r
            self._stop.wait(POLL_INTERVAL)

    def _read_4_20ma(self, channel: int, min_val: float, max_val: float) -> float:
        ma = self._hat.get_crt(channel) * 1000.0  # A → mA
        ma = max(4.0, min(20.0, ma))               # clamp to valid range
        return min_val + (ma - 4.0) / 16.0 * (max_val - min_val)
