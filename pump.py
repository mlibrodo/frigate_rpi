#!/usr/bin/env python3
"""
Frigate -> Roboflow pump (multi-camera)

What it does
- Pull latest JPEG from Frigate for each configured camera
- Save last N frames in /tmp/<camera>/frame_0.jpg ... frame_{N-1}.jpg (rotating)
- POST to local Roboflow inference server dataset/version endpoint
- Print JSON lines with camera/top/confidence/latency
- Optional: only emit on change
- Optional: ember alerting with threshold + debounce window per camera

Env vars
  ROBOFLOW_API_KEY    required
  MODEL_ID            default: ember-training-poc/2          (dataset/version)
  ROBOFLOW_URL        default: http://127.0.0.1:9001
  FRIGATE_URL         default: http://192.168.1.102:5000
  FRIGATE_CAMERAS     default: tahoe_cam1,tahoe_cam2  (comma-separated)

  PUMP_FPS            default: 2.0     (soft rate limit per camera)
  MIN_INTERVAL_S      default: 0       (hard min seconds per loop; overrides if larger)
  KEEP_FRAMES         default: 5       (rotating frames kept in /tmp/<camera>/)

  EMIT_ON_CHANGE      default: 0       (1 => only print prediction when top/conf changes)
  EMBER_LABEL         default: ember
  EMBER_THRESHOLD     default: 0.85
  DEBOUNCE_HITS       default: 3
  DEBOUNCE_WINDOW     default: 5
"""

from __future__ import annotations

import json
import os
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Deque, Dict, Optional, Tuple

import requests


def _env_bool(name: str, default: bool = False) -> bool:
    v = os.environ.get(name)
    if v is None:
        return default
    return v.strip().lower() in {"1", "true", "yes", "y", "on"}


@dataclass(frozen=True)
class Config:
    frigate_url: str
    frigate_cameras: list          # list of camera names
    roboflow_url: str
    model_id: str                  # "dataset/version"
    api_key: str

    fps: float
    min_interval_s: float
    keep_frames: int

    emit_on_change: bool

    ember_label: str
    ember_threshold: float
    debounce_hits: int
    debounce_window: int

    request_timeout_s: float = 10.0
    frame_timeout_s: float = 5.0
    retry_backoff_s: float = 1.0


def parse_config() -> Config:
    api_key = os.environ.get("ROBOFLOW_API_KEY", "B5p60bLPJYURpEpoGHcc")

    cameras_raw = os.environ.get("FRIGATE_CAMERAS", "tahoe_cam1,tahoe_cam2")
    cameras = [c.strip() for c in cameras_raw.split(",") if c.strip()]
    if not cameras:
        cameras = ["tahoe_cam1"]

    keep_frames = int(os.environ.get("KEEP_FRAMES", "5"))
    if keep_frames < 0:
        keep_frames = 0

    fps = float(os.environ.get("PUMP_FPS", "2.0"))
    if fps <= 0:
        fps = 0.1

    min_interval_s = float(os.environ.get("MIN_INTERVAL_S", "0"))
    if min_interval_s < 0:
        min_interval_s = 0

    debounce_window = int(os.environ.get("DEBOUNCE_WINDOW", "5"))
    if debounce_window < 1:
        debounce_window = 1

    debounce_hits = int(os.environ.get("DEBOUNCE_HITS", "3"))
    if debounce_hits < 1:
        debounce_hits = 1
    if debounce_hits > debounce_window:
        debounce_hits = debounce_window

    return Config(
        frigate_url=os.environ.get("FRIGATE_URL", "http://192.168.1.102:5000").rstrip("/"),
        frigate_cameras=cameras,
        roboflow_url=os.environ.get("ROBOFLOW_URL", "http://127.0.0.1:9001").rstrip("/"),
        model_id=os.environ.get("MODEL_ID", "ember-training-poc/2").strip("/"),
        api_key=api_key,
        fps=fps,
        min_interval_s=min_interval_s,
        keep_frames=keep_frames,
        emit_on_change=_env_bool("EMIT_ON_CHANGE", False),
        ember_label=os.environ.get("EMBER_LABEL", "ember"),
        ember_threshold=float(os.environ.get("EMBER_THRESHOLD", "0.85")),
        debounce_hits=debounce_hits,
        debounce_window=debounce_window,
    )


def build_infer_url(cfg: Config) -> str:
    return f"{cfg.roboflow_url}/{cfg.model_id}"


def init_frame_dir(camera: str) -> Path:
    d = Path("/tmp") / camera
    d.mkdir(parents=True, exist_ok=True)
    return d


def save_rotating_frame(frame_dir: Path, img_bytes: bytes, max_frames: int, idx: int) -> int:
    if max_frames <= 0:
        return idx
    path = frame_dir / f"frame_{idx % max_frames}.jpg"
    path.write_bytes(img_bytes)
    latest = frame_dir / "latest.jpg"
    try:
        if latest.exists() or latest.is_symlink():
            latest.unlink()
        latest.symlink_to(path.name)
    except Exception:
        pass
    return idx + 1


def fetch_frame(session: requests.Session, url: str, timeout_s: float) -> bytes:
    r = session.get(url, timeout=timeout_s)
    r.raise_for_status()
    return r.content


def infer_classification(
    session: requests.Session,
    infer_url: str,
    api_key: str,
    img_bytes: bytes,
    timeout_s: float,
) -> dict:
    files = {"file": ("frame.jpg", img_bytes, "image/jpeg")}
    r = session.post(f"{infer_url}?api_key={api_key}", files=files, timeout=timeout_s)
    r.raise_for_status()
    return r.json()


def is_ember(pred: dict, ember_label: str, threshold: float) -> bool:
    top = pred.get("top")
    conf = pred.get("confidence")
    try:
        conf_f = float(conf) if conf is not None else 0.0
    except (TypeError, ValueError):
        conf_f = 0.0
    return (top == ember_label) and (conf_f >= threshold)


# Per-camera state
@dataclass
class CameraState:
    name: str
    frame_dir: Path
    frame_idx: int = 0
    ember_window: Deque[bool] = field(default_factory=lambda: deque(maxlen=5))
    ember_alert_active: bool = False
    last_emit_key: Optional[str] = None


def process_camera(
    session: requests.Session,
    cfg: Config,
    state: CameraState,
    infer_url: str,
) -> None:
    """Run one inference cycle for a single camera."""
    t0 = time.time()
    camera = state.name
    latest_url = f"{cfg.frigate_url}/api/{camera}/latest.jpg"

    try:
        # 1) fetch JPEG from Frigate
        img = fetch_frame(session, latest_url, cfg.frame_timeout_s)

        # 2) save rotating frames for debugging
        state.frame_idx = save_rotating_frame(
            frame_dir=state.frame_dir,
            img_bytes=img,
            max_frames=cfg.keep_frames,
            idx=state.frame_idx,
        )

        # 3) run inference
        pred = infer_classification(session, infer_url, cfg.api_key, img, cfg.request_timeout_s)

        top = pred.get("top")
        conf = pred.get("confidence")
        latency_s = time.time() - t0

        ember_hit = is_ember(pred, cfg.ember_label, cfg.ember_threshold)
        state.ember_window.append(ember_hit)
        hits = sum(1 for x in state.ember_window if x)

        # Debounced alert logic
        ember_now = hits >= cfg.debounce_hits
        if ember_now and not state.ember_alert_active:
            state.ember_alert_active = True
            print(json.dumps({
                "event": "EMBER_ALERT_ON",
                "camera": camera,
                "top": top,
                "confidence": conf,
                "hits": hits,
                "window": len(state.ember_window),
                "latency_s": round(latency_s, 3),
                "frame_dir": str(state.frame_dir),
            }, separators=(",", ":")))
        elif (not ember_now) and state.ember_alert_active:
            state.ember_alert_active = False
            print(json.dumps({
                "event": "EMBER_ALERT_OFF",
                "camera": camera,
                "hits": hits,
                "window": len(state.ember_window),
            }, separators=(",", ":")))

        emit_key = f"{top}:{conf}"
        if (not cfg.emit_on_change) or (emit_key != state.last_emit_key):
            state.last_emit_key = emit_key
            print(json.dumps({
                "event": "prediction",
                "camera": camera,
                "top": top,
                "confidence": conf,
                "ember_hit": ember_hit,
                "hits": hits,
                "window": len(state.ember_window),
                "latency_s": round(latency_s, 3),
            }, separators=(",", ":")))

    except Exception as e:
        print(json.dumps({
            "event": "error",
            "camera": camera,
            "error": str(e),
        }, separators=(",", ":")))
        time.sleep(cfg.retry_backoff_s)


def main() -> None:
    cfg = parse_config()
    infer_url = build_infer_url(cfg)

    # soft rate limit via fps
    sleep_target = max(0.001, 1.0 / max(cfg.fps, 0.001))

    session = requests.Session()
    session.headers.update({"User-Agent": "frigate-roboflow-pump/1.1"})

    # Initialize per-camera state
    camera_states: Dict[str, CameraState] = {}
    for cam in cfg.frigate_cameras:
        camera_states[cam] = CameraState(
            name=cam,
            frame_dir=init_frame_dir(cam),
            ember_window=deque(maxlen=cfg.debounce_window),
        )

    print(json.dumps({
        "event": "start",
        "frigate_url": cfg.frigate_url,
        "cameras": cfg.frigate_cameras,
        "roboflow_infer": infer_url,
        "fps": cfg.fps,
        "min_interval_s": cfg.min_interval_s,
        "keep_frames": cfg.keep_frames,
        "emit_on_change": cfg.emit_on_change,
        "ember_label": cfg.ember_label,
        "ember_threshold": cfg.ember_threshold,
        "debounce_hits": cfg.debounce_hits,
        "debounce_window": cfg.debounce_window,
    }, separators=(",", ":")))

    while True:
        t0 = time.time()

        # Process each camera in sequence
        for cam, state in camera_states.items():
            process_camera(session, cfg, state, infer_url)

        # Pacing (FPS soft limit + hard min interval)
        elapsed = time.time() - t0
        sleep_fps = max(0.0, sleep_target - elapsed)
        sleep_min = max(0.0, cfg.min_interval_s - elapsed)
        time.sleep(max(sleep_fps, sleep_min))


if __name__ == "__main__":
    main()
