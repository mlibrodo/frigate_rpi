"""
web_panel.py — Pyregon remote dashboard
FastAPI + WebSocket server embedded as a daemon thread inside control_panel.py.

CPU design:
  - Zero polling loops. State is pushed only when control_panel.py calls
    push_state() or push_event() — typically on change, not on a timer.
  - When no clients are connected, push_state() / push_event() return
    immediately — no serialisation, no network I/O.
  - uvicorn sleeps in select() between events; idle cost ≈ 0%.

Security:
  - Intended to sit behind Cloudflare Access (handles auth at the edge).
  - The Pi never sees unauthenticated requests.
"""

import asyncio
import json
import logging
import sqlite3
import threading
from datetime import datetime
from pathlib import Path

import uvicorn
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel, confloat
from config import config as _cfg

log = logging.getLogger("web_panel")

DB_PATH  = str(Path(__file__).parent / "pyregon_events.db")
WEB_PORT = 8080
_HERE    = Path(__file__).parent


class WebPanel:
    def __init__(self, *,
                 get_state,      # () → dict   — snapshot of all current state
                 engine_start,   # ()          — queue start on Tkinter thread
                 engine_stop,    # ()          — queue stop on Tkinter thread
                 zone_toggle,    # (zone_id)   — queue toggle on Tkinter thread
                 mode_toggle,    # ()          — queue mode toggle on Tkinter thread
                 all_off):       # ()          — queue emergency off on Tkinter thread
        self._get_state   = get_state
        self._engine_start = engine_start
        self._engine_stop  = engine_stop
        self._zone_toggle  = zone_toggle
        self._mode_toggle  = mode_toggle
        self._all_off      = all_off

        self._loop: asyncio.AbstractEventLoop | None = None
        self._clients: set[WebSocket] = set()
        self._app = self._build_app()
        self._init_db()

    # ── SQLite event log ──────────────────────────────────────────────────────

    def _init_db(self):
        try:
            with sqlite3.connect(DB_PATH) as conn:
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS events (
                        id       INTEGER PRIMARY KEY AUTOINCREMENT,
                        ts       DATETIME DEFAULT CURRENT_TIMESTAMP,
                        category TEXT,
                        message  TEXT
                    )
                """)
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts)")
        except Exception as e:
            log.error(f"DB init failed: {e}")

    def _db_write(self, message: str):
        cat = ("engine" if any(w in message for w in ("Engine", "engine", "RUNNING", "STOPPED")) else
               "zone"   if "Zone" in message else
               "auto"   if "[AUTO]" in message else
               "ember"  if "ember" in message.lower() else
               "pump"   if "[PUMP]" in message else "info")
        try:
            with sqlite3.connect(DB_PATH) as conn:
                conn.execute(
                    "INSERT INTO events (category, message) VALUES (?, ?)",
                    (cat, message))
        except Exception as e:
            log.error(f"DB write failed: {e}")

    def _db_recent(self, limit: int = 100) -> list[dict]:
        try:
            with sqlite3.connect(DB_PATH) as conn:
                conn.row_factory = sqlite3.Row
                rows = conn.execute(
                    "SELECT ts, category, message FROM events "
                    "ORDER BY id DESC LIMIT ?", (limit,)
                ).fetchall()
                return [dict(r) for r in reversed(rows)]
        except Exception:
            return []

    # ── FastAPI routes ────────────────────────────────────────────────────────

    def _build_app(self) -> FastAPI:
        app = FastAPI(title="Pyregon Control", docs_url=None, redoc_url=None)

        @app.get("/", response_class=HTMLResponse)
        async def index():
            return (_HERE / "static" / "index.html").read_text()

        @app.get("/api/state")
        async def api_state():
            return self._get_state()

        @app.get("/api/events")
        async def api_events(limit: int = 100):
            return self._db_recent(min(limit, 500))

        @app.post("/api/engine/start")
        async def api_engine_start():
            self._engine_start()
            return {"ok": True}

        @app.post("/api/engine/stop")
        async def api_engine_stop():
            self._engine_stop()
            return {"ok": True}

        @app.post("/api/zone/{zone_id}/toggle")
        async def api_zone_toggle(zone_id: int):
            if zone_id not in (1, 2, 3, 4):
                return {"ok": False, "error": "invalid zone"}
            self._zone_toggle(zone_id)
            return {"ok": True}

        @app.post("/api/mode/toggle")
        async def api_mode_toggle():
            self._mode_toggle()
            return {"ok": True}

        @app.post("/api/emergency")
        async def api_emergency():
            self._all_off()
            return {"ok": True}

        @app.get("/api/settings/zones")
        async def api_get_zones():
            return _cfg.get("zones")

        class ZoneCoords(BaseModel):
            lat: float
            lon: float

        @app.post("/api/settings/zone/{zone_id}")
        async def api_set_zone(zone_id: int, coords: ZoneCoords):
            if zone_id not in (1, 2, 3, 4):
                return {"ok": False, "error": "invalid zone"}
            zones = _cfg.get("zones")
            for z in zones:
                if z["zone_id"] == zone_id:
                    z["lat"] = round(coords.lat, 7)
                    z["lon"] = round(coords.lon, 7)
                    break
            _cfg.set("zones", zones)
            log.info(f"Zone {zone_id} GPS set to ({coords.lat:.6f}, {coords.lon:.6f})")
            return {"ok": True}

        @app.websocket("/ws")
        async def ws_endpoint(ws: WebSocket):
            await ws.accept()
            self._clients.add(ws)
            log.info(f"WS client connected ({len(self._clients)} active)")
            try:
                # Send full state immediately on connect
                await ws.send_text(json.dumps(self._get_state()))
                # Replay last 50 events so new clients see history
                for ev in self._db_recent(50):
                    await ws.send_text(json.dumps({
                        "type": "event",
                        "ts":   ev["ts"][-8:],   # HH:MM:SS portion
                        "msg":  ev["message"],
                    }))
                # Hold connection open; drain any client messages (none expected)
                while True:
                    await asyncio.wait_for(ws.receive_text(), timeout=30)
            except (WebSocketDisconnect, asyncio.TimeoutError):
                pass
            except Exception:
                pass
            finally:
                self._clients.discard(ws)
                log.info(f"WS client disconnected ({len(self._clients)} active)")

        return app

    # ── Public API called from control_panel.py (any thread) ─────────────────

    def push_state(self):
        """Broadcast current state snapshot to all connected clients."""
        if not self._loop or not self._clients:
            return
        state = self._get_state()
        asyncio.run_coroutine_threadsafe(self._broadcast(state), self._loop)

    def push_event(self, msg: str):
        """Persist event to DB and broadcast to connected clients."""
        self._db_write(msg)
        if not self._loop or not self._clients:
            return
        ts = datetime.now().strftime("%H:%M:%S")
        asyncio.run_coroutine_threadsafe(
            self._broadcast({"type": "event", "ts": ts, "msg": msg}),
            self._loop)

    # ── Internal ──────────────────────────────────────────────────────────────

    async def _broadcast(self, data: dict):
        if not self._clients:
            return
        msg  = json.dumps(data)
        dead = set()
        for ws in list(self._clients):
            try:
                await ws.send_text(msg)
            except Exception:
                dead.add(ws)
        self._clients -= dead

    def start(self, host: str = "0.0.0.0", port: int = WEB_PORT):
        def _run():
            self._loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self._loop)
            config = uvicorn.Config(
                self._app, host=host, port=port, log_level="warning")
            server = uvicorn.Server(config)
            server.install_signal_handlers = lambda: None  # let control_panel own signals
            self._loop.run_until_complete(server.serve())

        threading.Thread(target=_run, daemon=True, name="web-panel").start()
        log.info(f"Web panel starting on http://0.0.0.0:{port}")
