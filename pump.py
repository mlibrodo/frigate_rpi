#!/usr/bin/env python3
"""
Frigate -> Roboflow pump (Jetson/offline friendly)

What it does
- Pull latest JPEG from Frigate camera endpoint
- Save last N frames in /tmp/<camera>/frame_0.jpg ... frame_{N-1}.jpg (rotating)
- POST to local Roboflow inference server dataset/version endpoint
- Print JSON lines with top/confidence/latency
- Optional: only emit on change
- Optional: ember alerting with threshold + debounce window

Env vars
  ROBOFLOW_API_KEY    required
  MODEL_ID            default: ember-training-poc/1          (dataset/version)
  ROBOFLOW_URL        default: http://127.0.0.1:9001
  FRIGATE_URL         default: http://127.0.0.1:5000
  FRIGATE_CAMERA      default: c920

  PUMP_FPS            default: 2.0     (soft rate limit)
  MIN_INTERVAL_S      default: 0       (hard min seconds per loop; overrides if larger)
  KEEP_FRAMES         default: 5       (rotating frames kept in /tmp/<camera>/)

  EMIT_ON_CHANGE      default: 0       (1 => only print prediction when top/conf changes)
  EMBER_LABEL         default: ember
  EMBER_THRESHOLD     default: 0.85
  DEBOUNCE_HITS       default: 3
  DEBOUNCE_WINDOW     default: 5

Notes
- Uses the proven working Roboflow request:
    POST http://127.0.0.1:9001/<dataset>/<version>
    Authorization: Bearer <api_key>
    multipart field: file=@frame.jpg
"""

from __future__ import annotations

import json
import os
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Deque, Optional, Tuple

import requests


def _env_bool(name: str, default: bool = False) -> bool:
    v = os.environ.get(name)
    if v is None:
        return default
    return v.strip().lower() in {"1", "true", "yes", "y", "on"}


@dataclass(frozen=True)
class Config:
    frigate_url: str
    frigate_camera: str
    roboflow_url: str
    model_id: str  # "dataset/version"
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
    api_key = os.environ.get("ROBOFLOW_API_KEY")
    if not api_key:
        raise SystemExit("ROBOFLOW_API_KEY is not set")

    keep_frames = int(os.environ.get("KEEP_FRAMES", "5"))
    if keep_frames < 0:
        keep_frames = 0

    fps = float(os.environ.get("PUMP_FPS", "2.0"))
    # prevent divide-by-zero / weird negatives
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
        # clamp so it can actually be reached
        debounce_hits = debounce_window

    return Config(
        frigate_url=os.environ.get("FRIGATE_URL", "http://192.168.1.102:5000").rstrip("/"),
        frigate_camera=os.environ.get("FRIGATE_CAMERA", "tahoe_cam_1"),
        roboflow_url=os.environ.get("ROBOFLOW_URL", "http://127.0.0.1:9001").rstrip("/"),
        model_id=os.environ.get("MODEL_ID", "ember-training-poc/1").strip("/"),
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


def build_urls(cfg: Config) -> Tuple[str, str]:
    latest_url = f"{cfg.frigate_url}/api/{cfg.frigate_camera}/latest.jpg"
    infer_url = f"{cfg.roboflow_url}/{cfg.model_id}"
    return latest_url, infer_url


def init_frame_dir(camera: str) -> Path:
    d = Path("/tmp") / camera
    d.mkdir(parents=True, exist_ok=True)
    return d


def save_rotating_frame(frame_dir: Path, img_bytes: bytes, max_frames: int, idx: int) -> int:
    if max_frames <= 0:
        return idx
    path = frame_dir / f"frame_{idx % max_frames}.jpg"
    path.write_bytes(img_bytes)
    # also update a convenient latest.jpg symlink if possible
    latest = frame_dir / "latest.jpg"
    try:
        if latest.exists() or latest.is_symlink():
            latest.unlink()
        latest.symlink_to(path.name)
    except Exception:
        # symlink may fail on some setups; ignore
        pass
    return idx + 1


def fetch_frame(session: requests.Session, latest_url: str, timeout_s: float) -> bytes:
    r = session.get(latest_url, timeout=timeout_s)
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


def main() -> None:
    cfg = parse_config()
    latest_url, infer_url = build_urls(cfg)

    # soft rate limit via fps
    sleep_target = max(0.001, 1.0 / max(cfg.fps, 0.001))

    session = requests.Session()
    session.headers.update({"User-Agent": "frigate-roboflow-pump/1.1"})

    frame_dir = init_frame_dir(cfg.frigate_camera)
    frame_idx = 0

    ember_window: Deque[bool] = deque(maxlen=cfg.debounce_window)
    ember_alert_active = False
    last_emit_key: Optional[str] = None

    print(
        json.dumps(
            {
                "event": "start",
                "frigate_latest": latest_url,
                "roboflow_infer": infer_url,
                "fps": cfg.fps,
                "min_interval_s": cfg.min_interval_s,
                "keep_frames": cfg.keep_frames,
                "frame_dir": str(frame_dir),
                "emit_on_change": cfg.emit_on_change,
                "ember_label": cfg.ember_label,
                "ember_threshold": cfg.ember_threshold,
                "debounce_hits": cfg.debounce_hits,
                "debounce_window": cfg.debounce_window,
            },
            separators=(",", ":"),
        )
    )

    while True:
        t0 = time.time()
        try:
            # 1) fetch JPEG from Frigate
            print('XXXX', latest_url)
            latest_url='http://192.168.1.102:5000/api/tahoe_cam1/latest.jpg'
            img = fetch_frame(session, latest_url, cfg.frame_timeout_s)

            print('YYYYY', latest_url)
            # 2) save rotating frames for debugging
            frame_idx = save_rotating_frame(
                frame_dir=frame_dir,
                img_bytes=img,
                max_frames=cfg.keep_frames,
                idx=frame_idx,
            )

            # 3) run inference
            pred = infer_classification(session, infer_url, cfg.api_key, img, cfg.request_timeout_s)

            top = pred.get("top")
            conf = pred.get("confidence")
            latency_s = time.time() - t0

            ember_hit = is_ember(pred, cfg.ember_label, cfg.ember_threshold)
            ember_window.append(ember_hit)
            hits = sum(1 for x in ember_window if x)

            # Debounced alert logic
            ember_now = hits >= cfg.debounce_hits
            if ember_now and not ember_alert_active:
                ember_alert_active = True
                print(
                    json.dumps(
                        {
                            "event": "EMBER_ALERT_ON",
                            "top": top,
                            "confidence": conf,
                            "hits": hits,
                            "window": len(ember_window),
                            "latency_s": round(latency_s, 3),
                            "frame_dir": str(frame_dir),
                        },
                        separators=(",", ":"),
                    )
                )
            elif (not ember_now) and ember_alert_active:
                ember_alert_active = False
                print(
                    json.dumps(
                        {"event": "EMBER_ALERT_OFF", "hits": hits, "window": len(ember_window)},
                        separators=(",", ":"),
                    )
                )

            emit_key = f"{top}:{conf}"
            if (not cfg.emit_on_change) or (emit_key != last_emit_key):
                last_emit_key = emit_key
                print(
                    json.dumps(
                        {
                            "event": "prediction",
                            "top": top,
                            "confidence": conf,
                            "ember_hit": ember_hit,
                            "hits": hits,
                            "window": len(ember_window),
                            "latency_s": round(latency_s, 3),
                        },
                        separators=(",", ":"),
                    )
                )

        except Exception as e:
            print(json.dumps({"event": "error", "error": str(e)}, separators=(",", ":")))
            time.sleep(cfg.retry_backoff_s)

        # pacing (FPS soft limit + hard min interval)
        elapsed = time.time() - t0
        sleep_fps = max(0.0, sleep_target - elapsed)
        sleep_min = max(0.0, cfg.min_interval_s - elapsed)
        time.sleep(max(sleep_fps, sleep_min))


if __name__ == "__main__":
    main()
