"""
anemometer.py — Renke RS-CFSFX-N01-*EX Anemometer Driver
Modbus RTU over RS-485. Implements the Anemometer interface
expected by AutoModeController in auto_mode.py.

Hardware specs (from manual):
  - Output: RS-485, Modbus-RTU
  - Default address: 0x01
  - Default baud rate: 4800 bps
  - Data bits: 8, Parity: None, Stop bits: 1
  - Wind speed register 0x0000: raw value × 100 (e.g. 125 → 1.25 m/s)
  - Wind direction register 0x0001: integer degrees, 0=N, clockwise
  - Max wind speed register 0x0002: raw value × 100
  - Wind rating register 0x0003: Beaufort scale integer (0–17)

Wiring:
  Brown → 10-30V DC positive
  Black  → GND
  Yellow → RS-485 A
  Blue   → RS-485 B
"""

import struct
import threading
import time
import logging

try:
    import minimalmodbus
    MODBUS_AVAILABLE = True
except ImportError:
    MODBUS_AVAILABLE = False

logger = logging.getLogger(__name__)

# ── Unit conversion ───────────────────────────────────────────────────────────
MS_TO_MPH = 2.23694  # 1 m/s = 2.23694 mph


def ms_to_mph(ms: float) -> float:
    return ms * MS_TO_MPH


# ── Register map ──────────────────────────────────────────────────────────────
REG_WIND_SPEED     = 0x0000   # Raw × 100, read only
REG_WIND_DIRECTION = 0x0001   # Degrees integer, read only
REG_MAX_WIND_SPEED = 0x0002   # Raw × 100, read only
REG_WIND_RATING    = 0x0003   # Beaufort 0–17, read only
REG_DEVICE_ADDRESS = 0x07D0   # 1–254, read/write
REG_BAUD_RATE      = 0x07D1   # 0=2400, 1=4800, 2=9600, read/write

BAUD_MAP = {0: 2400, 1: 4800, 2: 9600}


class AnemometerReading:
    """Snapshot of one poll cycle."""
    def __init__(self, speed_ms, direction_deg, max_speed_ms, wind_rating, timestamp):
        self.speed_ms       = speed_ms        # m/s (float)
        self.speed_mph      = ms_to_mph(speed_ms)
        self.direction_deg  = direction_deg   # 0–360°, 0=N, clockwise
        self.max_speed_ms   = max_speed_ms    # m/s since power-on
        self.wind_rating    = wind_rating     # Beaufort integer
        self.timestamp      = timestamp

    def cardinal(self) -> str:
        """Return compass cardinal/intercardinal label for wind direction."""
        dirs = ["N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE",
                "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW"]
        idx = int((self.direction_deg + 11.25) / 22.5) % 16
        return dirs[idx]

    def __repr__(self):
        return (f"AnemometerReading(speed={self.speed_ms:.2f} m/s "
                f"[{self.speed_mph:.1f} mph], dir={self.direction_deg}° "
                f"[{self.cardinal()}], rating={self.wind_rating})")


class Anemometer:
    """
    Driver for Renke RS-CFSFX-N01-*EX ultrasonic anemometer.

    Implements the interface required by AutoModeController:
        is_online()         → bool
        get_wind_speed()    → float  (mph)
        get_wind_direction()→ float  (degrees, 0=N clockwise)

    Also exposes:
        get_reading()       → AnemometerReading | None
        start_polling(interval_sec)
        stop_polling()
    """

    def __init__(self,
                 port: str = "/dev/ttyUSB0",
                 device_address: int = 1,
                 baud_rate: int = 4800,
                 poll_interval: float = 1.0,
                 timeout: float = 1.0,
                 on_reading: callable = None):
        """
        Args:
            port:           Serial port (e.g. '/dev/ttyUSB0' or '/dev/ttyAMA0')
            device_address: Modbus device address (default 1)
            baud_rate:      Serial baud rate (default 4800)
            poll_interval:  Seconds between automatic polls (default 1s = sensor response time)
            timeout:        Serial read timeout in seconds
            on_reading:     Optional callback(AnemometerReading) on each successful read
        """
        self.port           = port
        self.device_address = device_address
        self.baud_rate      = baud_rate
        self.poll_interval  = poll_interval
        self.timeout        = timeout
        self.on_reading     = on_reading

        self._instrument    = None
        self._lock          = threading.Lock()
        self._latest        = None          # AnemometerReading | None
        self._online        = False
        self._consecutive_failures = 0
        self._failure_threshold    = 3      # failures before marking offline

        self._poll_thread   = None
        self._stop_event    = threading.Event()

    # ── AutoModeController interface ──────────────────────────────────────────

    def is_online(self) -> bool:
        return self._online

    def get_wind_speed(self) -> float:
        """Returns wind speed in mph. Returns 0.0 if offline."""
        with self._lock:
            if self._latest:
                return self._latest.speed_mph
        return 0.0

    def get_wind_direction(self) -> float:
        """Returns wind FROM direction in degrees (0=N, clockwise). Returns 0.0 if offline."""
        with self._lock:
            if self._latest:
                return float(self._latest.direction_deg)
        return 0.0

    def get_reading(self) -> AnemometerReading | None:
        """Returns the latest full reading snapshot, or None if not yet available."""
        with self._lock:
            return self._latest

    # ── Connection management ─────────────────────────────────────────────────

    def connect(self) -> bool:
        """Open the serial port and initialise the Modbus instrument."""
        if not MODBUS_AVAILABLE:
            logger.error("minimalmodbus not installed. Run: pip install minimalmodbus")
            return False
        try:
            instrument = minimalmodbus.Instrument(self.port, self.device_address)
            instrument.serial.baudrate = self.baud_rate
            instrument.serial.bytesize = 8
            instrument.serial.parity   = minimalmodbus.serial.PARITY_NONE
            instrument.serial.stopbits = 1
            instrument.serial.timeout  = self.timeout
            instrument.mode            = minimalmodbus.MODE_RTU
            instrument.close_port_after_each_call = False
            self._instrument = instrument
            logger.info(f"Anemometer connected on {self.port} "
                        f"(addr={self.device_address}, baud={self.baud_rate})")
            return True
        except Exception as e:
            logger.error(f"Anemometer connect failed: {e}")
            return False

    def disconnect(self):
        self.stop_polling()
        if self._instrument:
            try:
                self._instrument.serial.close()
            except Exception:
                pass
            self._instrument = None
        self._online = False
        logger.info("Anemometer disconnected.")

    # ── Single poll ───────────────────────────────────────────────────────────

    def poll(self) -> AnemometerReading | None:
        """
        Perform one Modbus read of all four registers in a single request.
        Returns an AnemometerReading on success, None on failure.

        Modbus request: function 0x03, start=0x0000, count=4
        Reads: wind_speed, wind_direction, max_wind_speed, wind_rating
        """
        if self._instrument is None:
            if not self.connect():
                self._mark_offline()
                return None

        try:
            with self._lock:
                # Read 4 consecutive registers starting at 0x0000
                raw = self._instrument.read_registers(
                    registeraddress=REG_WIND_SPEED,
                    number_of_registers=4,
                    functioncode=3
                )

            # Decode per manual:
            # reg[0] = wind speed × 100 (unsigned int)
            # reg[1] = wind direction in degrees (unsigned int, 0–360)
            # reg[2] = max wind speed × 100 (unsigned int)
            # reg[3] = wind rating Beaufort (unsigned int, 0–17)
            speed_ms      = raw[0] / 100.0
            direction_deg = raw[1]
            max_speed_ms  = raw[2] / 100.0
            wind_rating   = raw[3]

            reading = AnemometerReading(
                speed_ms      = speed_ms,
                direction_deg = direction_deg,
                max_speed_ms  = max_speed_ms,
                wind_rating   = wind_rating,
                timestamp     = time.time()
            )

            with self._lock:
                self._latest = reading
            self._mark_online()

            if self.on_reading:
                try:
                    self.on_reading(reading)
                except Exception as cb_err:
                    logger.warning(f"on_reading callback error: {cb_err}")

            return reading

        except Exception as e:
            logger.warning(f"Anemometer poll error: {e}")
            self._consecutive_failures += 1
            if self._consecutive_failures >= self._failure_threshold:
                self._mark_offline()
            return None

    # ── Polling loop ──────────────────────────────────────────────────────────

    def start_polling(self, interval: float = None):
        """Start background polling thread."""
        if self._poll_thread and self._poll_thread.is_alive():
            return
        if interval:
            self.poll_interval = interval
        self._stop_event.clear()
        self._poll_thread = threading.Thread(
            target=self._poll_loop, daemon=True, name="anemometer-poll"
        )
        self._poll_thread.start()
        logger.info(f"Anemometer polling started (interval={self.poll_interval}s).")

    def stop_polling(self):
        self._stop_event.set()
        if self._poll_thread:
            self._poll_thread.join(timeout=5)
        logger.info("Anemometer polling stopped.")

    def _poll_loop(self):
        while not self._stop_event.is_set():
            self.poll()
            self._stop_event.wait(timeout=self.poll_interval)

    # ── Status helpers ────────────────────────────────────────────────────────

    def _mark_online(self):
        if not self._online:
            logger.info("Anemometer is online.")
        self._online = True
        self._consecutive_failures = 0

    def _mark_offline(self):
        if self._online:
            logger.warning("Anemometer marked OFFLINE after consecutive failures.")
        self._online = False

    # ── Device configuration (read/write registers) ───────────────────────────

    def read_device_address(self) -> int | None:
        """Read the current Modbus device address from the sensor."""
        try:
            val = self._instrument.read_register(REG_DEVICE_ADDRESS, functioncode=3)
            return val
        except Exception as e:
            logger.error(f"read_device_address failed: {e}")
            return None

    def set_device_address(self, new_address: int) -> bool:
        """Write a new Modbus device address (1–254). Takes effect immediately."""
        if not 1 <= new_address <= 254:
            logger.error("Device address must be 1–254.")
            return False
        try:
            self._instrument.write_register(REG_DEVICE_ADDRESS, new_address, functioncode=6)
            logger.info(f"Anemometer device address set to {new_address}.")
            self.device_address = new_address
            self._instrument.address = new_address
            return True
        except Exception as e:
            logger.error(f"set_device_address failed: {e}")
            return False

    def read_baud_rate(self) -> int | None:
        """Read current baud rate code (0=2400, 1=4800, 2=9600)."""
        try:
            code = self._instrument.read_register(REG_BAUD_RATE, functioncode=3)
            return BAUD_MAP.get(code)
        except Exception as e:
            logger.error(f"read_baud_rate failed: {e}")
            return None

    def set_baud_rate(self, baud: int) -> bool:
        """Set baud rate. baud must be 2400, 4800, or 9600."""
        code_map = {v: k for k, v in BAUD_MAP.items()}
        if baud not in code_map:
            logger.error(f"Invalid baud rate {baud}. Choose from {list(code_map.keys())}.")
            return False
        try:
            self._instrument.write_register(REG_BAUD_RATE, code_map[baud], functioncode=6)
            logger.info(f"Anemometer baud rate set to {baud}.")
            return True
        except Exception as e:
            logger.error(f"set_baud_rate failed: {e}")
            return False


# ── Simulation stub (used when no hardware present) ───────────────────────────

class SimulatedAnemometer:
    """
    Drop-in replacement for Anemometer when no hardware is connected.
    Generates synthetic wind readings for development and testing.
    """
    import math as _math
    import random as _random

    def __init__(self, speed_mph=15.0, direction_deg=45.0):
        self._speed_mph   = speed_mph
        self._dir_deg     = direction_deg
        self._online      = True
        self._t           = 0

    def is_online(self) -> bool:
        return self._online

    def set_online(self, online: bool):
        self._online = online

    def get_wind_speed(self) -> float:
        import math, random
        self._t += 1
        return max(0.0, self._speed_mph + math.sin(self._t * 0.1) * 3 + random.uniform(-1, 1))

    def get_wind_direction(self) -> float:
        import random
        return (self._dir_deg + random.uniform(-5, 5)) % 360

    def get_reading(self):
        speed_mph = self.get_wind_speed()
        return AnemometerReading(
            speed_ms      = speed_mph / MS_TO_MPH,
            direction_deg = int(self.get_wind_direction()),
            max_speed_ms  = speed_mph / MS_TO_MPH,
            wind_rating   = min(17, int(speed_mph / 8)),
            timestamp     = time.time()
        )

    def poll(self):
        return self.get_reading()

    def start_polling(self, interval=1.0):
        pass

    def stop_polling(self):
        pass


# ── Quick diagnostic CLI ──────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.DEBUG,
                        format="%(asctime)s [%(levelname)s] %(message)s")

    port    = sys.argv[1] if len(sys.argv) > 1 else "/dev/ttyUSB0"
    address = int(sys.argv[2]) if len(sys.argv) > 2 else 1

    print(f"\nPyregon Anemometer Diagnostic")
    print(f"Port: {port}  |  Address: {address}  |  Baud: 4800\n")

    sensor = Anemometer(port=port, device_address=address, baud_rate=4800)
    if not sensor.connect():
        print("Could not connect. Check wiring and port.")
        sys.exit(1)

    print("Polling every 1s — press Ctrl+C to stop.\n")
    try:
        while True:
            r = sensor.poll()
            if r:
                print(f"  Speed: {r.speed_ms:.2f} m/s  ({r.speed_mph:.1f} mph)  |  "
                      f"Direction: {r.direction_deg}° ({r.cardinal()})  |  "
                      f"Beaufort: {r.wind_rating}  |  "
                      f"Max: {r.max_speed_ms:.2f} m/s")
            else:
                print("  [no reading]")
            time.sleep(1)
    except KeyboardInterrupt:
        print("\nDone.")
    finally:
        sensor.disconnect()
