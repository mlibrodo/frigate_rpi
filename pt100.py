"""
pt100.py — RS-485 PT100 Temperature Module Driver
Modbus RTU over RS-485, sharing the bus with the anemometer (see anemometer.py).

Hardware specs (reverse-engineered — module is unlabeled, no datasheet):
  - Output: RS-485, Modbus-RTU
  - Default address: 1
  - Default baud rate: 9600 bps
  - Data bits: 8, Parity: None, Stop bits: 1
  - Temperature register 0x0000: raw value × 10, signed (e.g. 282 → 28.2°C)
  - Does NOT use Renke's config register map (0x07D0 write returns a clean
    Modbus "illegal data address" exception, not a timeout) — no known way to
    reconfigure this module's address/baud, so no set_device_address()/
    set_baud_rate() here. If a bus address conflict needs resolving, change
    the anemometer's address instead (see Anemometer.set_device_address).

Wiring: shares the same RS-485 A/B pair as the anemometer (multi-drop bus).
Power leads unconfirmed — verify whether this module needs its own supply.
"""

import threading
import time
import logging

try:
    import minimalmodbus
    MODBUS_AVAILABLE = True
except ImportError:
    MODBUS_AVAILABLE = False

logger = logging.getLogger(__name__)

# ── Register map ───────────────────────────────────────────────────────────
REG_TEMPERATURE = 0x0000   # Raw × 10, signed, read only


class PT100Reading:
    """Snapshot of one poll cycle."""
    def __init__(self, temp_c, timestamp):
        self.temp_c    = temp_c
        self.temp_f    = temp_c * 9 / 5 + 32
        self.timestamp = timestamp

    def __repr__(self):
        return f"PT100Reading(temp={self.temp_c:.1f}°C [{self.temp_f:.1f}°F])"


class PT100:
    """
    Driver for the unlabeled RS-485 PT100 temperature module.

    Exposes:
        is_online()          → bool
        get_temperature_c()  → float
        get_temperature_f()  → float
        get_reading()        → PT100Reading | None
        start_polling(interval_sec)
        stop_polling()
    """

    def __init__(self,
                 port: str = "/dev/ttyAMA0",
                 device_address: int = 1,
                 baud_rate: int = 9600,
                 poll_interval: float = 1.0,
                 timeout: float = 0.5,
                 on_reading: callable = None):
        """
        Args:
            port:           Serial port (e.g. '/dev/ttyAMA0')
            device_address: Modbus device address (default 1)
            baud_rate:      Serial baud rate (default 9600)
            poll_interval:  Seconds between automatic polls
            timeout:        Serial read timeout in seconds
            on_reading:     Optional callback(PT100Reading) on each successful read
        """
        self.port           = port
        self.device_address = device_address
        self.baud_rate      = baud_rate
        self.poll_interval  = poll_interval
        self.timeout        = timeout
        self.on_reading     = on_reading

        self._instrument    = None
        self._lock          = threading.Lock()
        self._latest        = None          # PT100Reading | None
        self._online        = False
        self._consecutive_failures = 0
        self._failure_threshold    = 3      # failures before marking offline

        self._poll_thread   = None
        self._stop_event    = threading.Event()

    # ── Read interface ────────────────────────────────────────────────────

    def is_online(self) -> bool:
        return self._online

    def get_temperature_c(self) -> float:
        """Returns temperature in °C. Returns 0.0 if offline."""
        with self._lock:
            if self._latest:
                return self._latest.temp_c
        return 0.0

    def get_temperature_f(self) -> float:
        """Returns temperature in °F. Returns 32.0 if offline."""
        with self._lock:
            if self._latest:
                return self._latest.temp_f
        return 32.0

    def get_reading(self) -> PT100Reading | None:
        """Returns the latest full reading snapshot, or None if not yet available."""
        with self._lock:
            return self._latest

    # ── Connection management ─────────────────────────────────────────────

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
            logger.info(f"PT100 connected on {self.port} "
                        f"(addr={self.device_address}, baud={self.baud_rate})")
            return True
        except Exception as e:
            logger.error(f"PT100 connect failed: {e}")
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
        logger.info("PT100 disconnected.")

    # ── Single poll ────────────────────────────────────────────────────────

    def poll(self) -> PT100Reading | None:
        """
        Perform one Modbus read of the temperature register.
        Returns a PT100Reading on success, None on failure.

        Modbus request: function 0x03, register 0x0000, signed
        """
        if self._instrument is None:
            if not self.connect():
                self._mark_offline()
                return None

        try:
            with self._lock:
                raw = self._instrument.read_register(
                    registeraddress=REG_TEMPERATURE,
                    number_of_decimals=0,
                    functioncode=3,
                    signed=True
                )

            temp_c = raw / 10.0

            reading = PT100Reading(temp_c=temp_c, timestamp=time.time())

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
            logger.warning(f"PT100 poll error: {e}")
            self._consecutive_failures += 1
            if self._consecutive_failures >= self._failure_threshold:
                self._mark_offline()
            return None

    # ── Polling loop ──────────────────────────────────────────────────────

    def start_polling(self, interval: float = None):
        """Start background polling thread."""
        if self._poll_thread and self._poll_thread.is_alive():
            return
        if interval:
            self.poll_interval = interval
        self._stop_event.clear()
        self._poll_thread = threading.Thread(
            target=self._poll_loop, daemon=True, name="pt100-poll"
        )
        self._poll_thread.start()
        logger.info(f"PT100 polling started (interval={self.poll_interval}s).")

    def stop_polling(self):
        self._stop_event.set()
        if self._poll_thread:
            self._poll_thread.join(timeout=5)
        logger.info("PT100 polling stopped.")

    def _poll_loop(self):
        while not self._stop_event.is_set():
            self.poll()
            self._stop_event.wait(timeout=self.poll_interval)

    # ── Status helpers ────────────────────────────────────────────────────

    def _mark_online(self):
        if not self._online:
            logger.info("PT100 is online.")
        self._online = True
        self._consecutive_failures = 0

    def _mark_offline(self):
        if self._online:
            logger.warning("PT100 marked OFFLINE after consecutive failures.")
        self._online = False


# ── Simulation stub (used when no hardware present) ─────────────────────────

class SimulatedPT100:
    """Drop-in replacement for PT100 when no hardware is connected."""

    def __init__(self, temp_f=78.0):
        self._temp_f  = temp_f
        self._online  = True
        self._t       = 0

    def is_online(self) -> bool:
        return self._online

    def set_online(self, online: bool):
        self._online = online

    def get_temperature_f(self) -> float:
        import math, random
        self._t += 1
        return self._temp_f + math.sin(self._t * 0.05) * 2 + random.uniform(-0.3, 0.3)

    def get_temperature_c(self) -> float:
        return (self.get_temperature_f() - 32) * 5 / 9

    def get_reading(self):
        temp_f = self.get_temperature_f()
        return PT100Reading(temp_c=(temp_f - 32) * 5 / 9, timestamp=time.time())

    def poll(self):
        return self.get_reading()

    def start_polling(self, interval=1.0):
        pass

    def stop_polling(self):
        pass


# ── Quick diagnostic CLI ─────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.DEBUG,
                        format="%(asctime)s [%(levelname)s] %(message)s")

    port    = sys.argv[1] if len(sys.argv) > 1 else "/dev/ttyAMA0"
    address = int(sys.argv[2]) if len(sys.argv) > 2 else 1

    print(f"\nPyregon PT100 Diagnostic")
    print(f"Port: {port}  |  Address: {address}  |  Baud: 9600\n")

    sensor = PT100(port=port, device_address=address, baud_rate=9600)
    if not sensor.connect():
        print("Could not connect. Check wiring and port.")
        sys.exit(1)

    print("Polling every 1s — press Ctrl+C to stop.\n")
    try:
        while True:
            r = sensor.poll()
            if r:
                print(f"  Temp: {r.temp_c:.1f}°C  ({r.temp_f:.1f}°F)")
            else:
                print("  [no reading]")
            time.sleep(1)
    except KeyboardInterrupt:
        print("\nDone.")
    finally:
        sensor.disconnect()
