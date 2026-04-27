"""
frigate_client.py — Pyregon Frigate NVR Integration
Connects to Frigate's MQTT event stream and REST API.
Provides the detection interface expected by AutoModeController.

Frigate detection events arrive via MQTT topic:
    frigate/events  →  JSON payload with label, score, camera

The get_detections() method returns the current live detections
as a list of dicts: [{"camera_id": int, "label": str, "confidence": float}]

Camera ID mapping (fixed at install):
    Camera 1 → North  (0°)
    Camera 2 → East   (90°)
    Camera 3 → South  (180°)
    Camera 4 → West   (270°)
"""

import json
import logging
import threading
import time
from collections import defaultdict

logger = logging.getLogger(__name__)

# Camera name → ID mapping (configured at install)
# Keys must match the camera names configured in Frigate
CAMERA_NAME_TO_ID = {
    "camera_north": 1,
    "camera_east":  2,
    "camera_south": 3,
    "camera_west":  4,
    # Fallback aliases
    "cam1": 1, "cam2": 2, "cam3": 3, "cam4": 4,
}

EMBER_LABELS = {"ember", "fire", "smoke", "flame"}

# How long a detection stays "live" before expiring (seconds)
DETECTION_TTL = 5.0

try:
    import paho.mqtt.client as mqtt
    MQTT_AVAILABLE = True
except ImportError:
    MQTT_AVAILABLE = False


class FrigateDetection:
    """Single detection event with expiry."""
    def __init__(self, camera_id, label, confidence):
        self.camera_id  = camera_id
        self.label      = label.lower()
        self.confidence = confidence   # 0.0–1.0
        self.timestamp  = time.time()

    def is_alive(self):
        return (time.time() - self.timestamp) < DETECTION_TTL

    def to_dict(self):
        return {
            "camera_id":  self.camera_id,
            "label":      self.label,
            "confidence": self.confidence,
        }


class FrigateClient:
    """
    Connects to Frigate via MQTT and maintains a live detection window.

    Required by AutoModeController:
        get_detections() → list of {"camera_id": int, "label": str, "confidence": float}

    Also provides:
        get_max_ember_confidence()  → float (0–100 %)
        is_connected()              → bool
        start() / stop()
    """

    def __init__(self,
                 mqtt_host: str = "localhost",
                 mqtt_port: int = 1883,
                 mqtt_user: str = None,
                 mqtt_password: str = None,
                 frigate_topic: str = "frigate/events",
                 on_ember_detection: callable = None):
        """
        Args:
            mqtt_host:           Mosquitto broker host (usually localhost on the Pi)
            mqtt_port:           Broker port (default 1883)
            mqtt_user/password:  Optional MQTT auth
            frigate_topic:       Frigate MQTT event topic
            on_ember_detection:  Optional callback(FrigateDetection) on each ember event
        """
        self.mqtt_host          = mqtt_host
        self.mqtt_port          = mqtt_port
        self.mqtt_user          = mqtt_user
        self.mqtt_password      = mqtt_password
        self.frigate_topic      = frigate_topic
        self.on_ember_detection = on_ember_detection

        self._client        = None
        self._connected     = False
        self._lock          = threading.Lock()
        # camera_id → list of FrigateDetection
        self._detections    = defaultdict(list)

        # Background cleanup thread
        self._stop_event    = threading.Event()
        self._cleanup_thread = None

    # ── AutoModeController interface ──────────────────────────────────────────

    def get_detections(self) -> list:
        """
        Returns all live ember/fire/smoke detections as list of dicts.
        Expired detections are filtered out automatically.
        """
        with self._lock:
            result = []
            for cam_detections in self._detections.values():
                for d in cam_detections:
                    if d.is_alive() and d.label in EMBER_LABELS:
                        result.append(d.to_dict())
            return result

    def get_max_ember_confidence(self) -> float:
        """Returns highest confidence (0–100%) across all live ember detections."""
        detections = self.get_detections()
        if not detections:
            return 0.0
        return max(d["confidence"] for d in detections) * 100.0

    def is_connected(self) -> bool:
        return self._connected

    # ── Lifecycle ──────────────────────────────────────────────────────────────

    def start(self):
        if not MQTT_AVAILABLE:
            logger.error("paho-mqtt not installed. Run: pip install paho-mqtt")
            return False

        self._client = mqtt.Client(client_id="pyregon_frigate", clean_session=True)

        if self.mqtt_user:
            self._client.username_pw_set(self.mqtt_user, self.mqtt_password)

        self._client.on_connect    = self._on_connect
        self._client.on_disconnect = self._on_disconnect
        self._client.on_message    = self._on_message

        try:
            self._client.connect(self.mqtt_host, self.mqtt_port, keepalive=60)
            self._client.loop_start()
            logger.info(f"Frigate client connecting to {self.mqtt_host}:{self.mqtt_port}")
        except Exception as e:
            logger.error(f"Frigate MQTT connect failed: {e}")
            return False

        # Start cleanup thread
        self._stop_event.clear()
        self._cleanup_thread = threading.Thread(
            target=self._cleanup_loop, daemon=True, name="frigate-cleanup"
        )
        self._cleanup_thread.start()
        return True

    def stop(self):
        self._stop_event.set()
        if self._client:
            self._client.loop_stop()
            self._client.disconnect()
        self._connected = False
        logger.info("Frigate client stopped.")

    # ── MQTT callbacks ────────────────────────────────────────────────────────

    def _on_connect(self, client, userdata, flags, rc):
        if rc == 0:
            self._connected = True
            client.subscribe(self.frigate_topic)
            # Also subscribe to per-camera topics for richer data
            client.subscribe("frigate/+/+")
            logger.info(f"Frigate MQTT connected. Subscribed to '{self.frigate_topic}'")
        else:
            logger.error(f"Frigate MQTT connection refused (rc={rc})")

    def _on_disconnect(self, client, userdata, rc):
        self._connected = False
        if rc != 0:
            logger.warning(f"Frigate MQTT unexpected disconnect (rc={rc}). Will auto-reconnect.")

    def _on_message(self, client, userdata, msg):
        try:
            payload = json.loads(msg.payload.decode("utf-8"))
            self._process_event(payload, msg.topic)
        except json.JSONDecodeError:
            pass
        except Exception as e:
            logger.warning(f"Frigate message parse error: {e}")

    def _process_event(self, payload: dict, topic: str):
        """
        Parse Frigate event payload. Frigate sends events in two formats:

        1. frigate/events  →  {"type": "new"|"update"|"end", "after": {...}, "before": {...}}
        2. frigate/<camera>/<label>  →  score as payload

        We handle both.
        """
        # Format 1: frigate/events full event
        if "after" in payload:
            event = payload["after"]
            self._ingest_event(
                camera_name = event.get("camera", ""),
                label       = event.get("label", ""),
                confidence  = event.get("score", 0.0),
                event_type  = payload.get("type", "update"),
            )

        # Format 2: frigate/<camera>/<label> score topic
        elif topic.startswith("frigate/") and topic.count("/") == 2:
            parts = topic.split("/")
            camera_name = parts[1]
            label       = parts[2]
            confidence  = float(payload) if isinstance(payload, (int, float)) else \
                          payload.get("score", 0.0)
            self._ingest_event(camera_name, label, confidence, "update")

    def _ingest_event(self, camera_name: str, label: str,
                      confidence: float, event_type: str):
        label = label.lower()
        if label not in EMBER_LABELS:
            return

        # Resolve camera name → ID
        camera_id = CAMERA_NAME_TO_ID.get(camera_name.lower())
        if camera_id is None:
            # Try extracting a trailing digit (e.g. "backyard_cam3" → 3)
            for ch in reversed(camera_name):
                if ch.isdigit() and 1 <= int(ch) <= 4:
                    camera_id = int(ch)
                    break
            if camera_id is None:
                camera_id = 1  # Safe fallback
                logger.warning(f"Unknown camera name '{camera_name}', defaulting to ID 1")

        if event_type == "end":
            # Remove detections from this camera for this label
            with self._lock:
                self._detections[camera_id] = [
                    d for d in self._detections[camera_id]
                    if d.label != label
                ]
            return

        detection = FrigateDetection(camera_id, label, confidence)

        with self._lock:
            # Replace any existing detection of same label/camera with fresh one
            self._detections[camera_id] = [
                d for d in self._detections[camera_id] if d.label != label
            ]
            self._detections[camera_id].append(detection)

        logger.debug(f"Detection: cam{camera_id} {label} {confidence*100:.1f}%")

        if self.on_ember_detection:
            try:
                self.on_ember_detection(detection)
            except Exception as e:
                logger.warning(f"on_ember_detection callback error: {e}")

    # ── Cleanup loop ──────────────────────────────────────────────────────────

    def _cleanup_loop(self):
        """Periodically remove expired detections."""
        while not self._stop_event.is_set():
            with self._lock:
                for cam_id in list(self._detections.keys()):
                    self._detections[cam_id] = [
                        d for d in self._detections[cam_id] if d.is_alive()
                    ]
            self._stop_event.wait(timeout=2.0)

    # ── Manual injection (for testing / simulation) ───────────────────────────

    def inject_detection(self, camera_id: int, label: str, confidence: float):
        """Directly inject a detection — useful for testing without Frigate running."""
        self._ingest_event(
            camera_name = f"cam{camera_id}",
            label       = label,
            confidence  = confidence,
            event_type  = "update"
        )


class SimulatedFrigateClient:
    """
    Drop-in replacement for FrigateClient when Frigate is not running.
    Allows manual injection of detections for AUTO mode testing.
    """

    def __init__(self):
        self._detections = []
        self._lock = threading.Lock()

    def get_detections(self) -> list:
        with self._lock:
            return [d for d in self._detections if d.is_alive()]

    def get_max_ember_confidence(self) -> float:
        detections = self.get_detections()
        if not detections:
            return 0.0
        return max(d["confidence"] for d in detections) * 100.0

    def is_connected(self) -> bool:
        return True

    def inject_detection(self, camera_id: int, label: str = "ember",
                         confidence: float = 0.95):
        with self._lock:
            self._detections = [d for d in self._detections
                                if not (d.camera_id == camera_id and d.label == label)]
            self._detections.append(FrigateDetection(camera_id, label, confidence))

    def clear_detections(self):
        with self._lock:
            self._detections.clear()

    def start(self):
        pass

    def stop(self):
        pass
