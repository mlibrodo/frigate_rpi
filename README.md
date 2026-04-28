# Pyregon Wildfire Protection System

Autonomous wildfire suppression controller running on a Raspberry Pi 5 with Hailo AI accelerator. Monitors up to four camera feeds for ember/fire detection, and automatically starts a gas engine pump and sequences irrigation zones based on wind direction and threat level.

Passwords and `ROBOFLOW_API_KEY` are in the [shared Google Doc](https://docs.google.com/document/d/1Ijb5Ih6niiNby-Oxc1I2EeX49zCnluoEAXFwS41Kqhk/edit?usp=sharing).

---

## Hardware

| Component | Details |
|-----------|---------|
| Raspberry Pi 5 | Hostname `pi-hailo`, static IP `192.168.1.153` |
| Hailo-8L AI accelerator | M.2 HAT, used by Frigate for object detection |
| Sequent Microsystems 8-Relay HAT | Controls engine (choke, starter, ignition kill) and 4 irrigation zones |
| Renke RS-CFSFX-N01 anemometer | Modbus RTU over `/dev/ttyUSB0`, 4800 baud, provides wind speed + direction |
| 800×480 touchscreen | Local Tkinter control panel |
| IP cameras | 4× RTSP cameras (tahoe_cam1–4), North/East/South/West |

---

## System Architecture & Data Flow

```
IP Cameras (RTSP)
      │
      ▼
┌─────────────┐   JPEG frames    ┌──────────────────────────┐
│   Frigate   │ ──────────────▶  │        pump.py           │
│  (Docker)   │  /api/<cam>/     │  (subprocess of          │
│  :5000      │  latest.jpg      │   control_panel.py)      │
└─────────────┘                  └──────────┬───────────────┘
                                            │ POST JPEG
                                            ▼
                                 ┌──────────────────────────┐
                                 │  Local Roboflow Server   │
                                 │  (Docker) :9001          │
                                 │  model: ember-training-  │
                                 │         poc/1            │
                                 └──────────┬───────────────┘
                                            │ JSON predictions
                                            │ (stdout lines)
                                            ▼
                                 ┌──────────────────────────┐
                                 │    control_panel.py      │
                                 │    (main process)        │
                                 │                          │
                                 │  ┌─────────────────────┐ │
                                 │  │  AutoModeController │ │ ◀── Anemometer
                                 │  │  (state machine)    │ │     (wind speed/dir)
                                 │  └──────────┬──────────┘ │
                                 │             │             │
                                 │  ┌──────────▼──────────┐ │
                                 │  │   Relay HAT         │ │
                                 │  │ Engine + 4 Zones    │ │
                                 │  └─────────────────────┘ │
                                 │                          │
                                 │  ┌─────────────────────┐ │
                                 │  │   Tkinter UI        │ │ ── 800×480 touchscreen
                                 │  └─────────────────────┘ │
                                 │                          │
                                 │  ┌─────────────────────┐ │
                                 │  │   web_panel.py      │ │ ── FastAPI :8080
                                 │  │   (daemon thread)   │ │
                                 │  └─────────────────────┘ │
                                 └──────────────────────────┘
```

### Step-by-step flow

1. **Frigate** (Docker, `:5000`) continuously ingests RTSP streams from four IP cameras and stores the latest JPEG frame per camera at `/api/<camera>/latest.jpg`.

2. **pump.py** is spawned as a subprocess by `control_panel.py`. It polls each camera's latest frame from Frigate at a configurable FPS (default 2 fps per camera), then POSTs each JPEG to the **local Roboflow inference server** (Docker, `:9001`) running the `ember-training-poc/1` classification model. Results are emitted as JSON lines on stdout.

3. **control_panel.py** reads pump.py's stdout in a background thread. Each prediction line is parsed and fed into:
   - The **AutoModeController** (`auto_mode.py`) — drives the state machine
   - The **Tkinter UI** — updates the ember confidence bar and log
   - The **WebPanel** (`web_panel.py`) — broadcasts state to remote clients

4. The **anemometer** is polled on its own thread via Modbus RTU. Wind speed and direction are used by the state machine to determine which irrigation zone to water first (upwind face gets priority). If the anemometer goes offline, the state machine falls back to camera confidence as a wind direction proxy — the camera with the highest sustained ember confidence is treated as facing the fire.

---

## AUTO Mode State Machine

```
IDLE ──▶ WATCHING ──▶ STARTING ──▶ PHASE1 ──▶ CYCLING ──▶ CLEARING ──▶ STOPPING
  ▲          │                                    │              │            │
  └──────────┘ (confidence drops)                └──────────────┘            │
  ▲                                                                           │
  └───────────────────────────────────────────────────────────────────────────┘
```

| State | Condition to enter | What happens |
|-------|--------------------|--------------|
| **IDLE** | Default / after stop | Monitoring only, nothing active |
| **WATCHING** | Ember confidence ≥ trigger threshold | Counting down sustained detection window |
| **STARTING** | Sustained ember detected | Engine start sequence (choke extend → ignition → starter → choke retract), up to N retries |
| **PHASE1** | Engine running | Upwind zone soaked for `initial_upwind_duration` seconds |
| **CYCLING** | Phase 1 complete | Clockwise zone cycling, duration weighted by wind speed and zone position |
| **CLEARING** | Ember confidence drops below clear threshold | Countdown before stopping (debounce against transient clears) |
| **STOPPING** | Clear countdown elapsed | All zones closed, engine stopped, return to IDLE |

Zone ordering is wind-aware: the zone facing the incoming wind gets water first, then the system cycles clockwise. Upwind and adjacent zones get longer run times in high-wind conditions; the downwind zone always gets baseline time.

---

## Relay HAT Map

| Relay | Function |
|-------|----------|
| 1 | Choke extend |
| 2 | Starter |
| 3 | Choke retract |
| 4 | Ignition kill |
| 5 | Zone 1 (North) |
| 6 | Zone 2 (East) |
| 7 | Zone 3 (South) |
| 8 | Zone 4 (West) |

---

## User Interfaces

### Local touchscreen (Tkinter)
Entry point: `python3 /home/librodo112/frigate_rpi/control_panel.py`

Runs on the Pi's 800×480 display. Provides engine controls, manual zone toggles, AUTO/MANUAL mode switch, live camera snapshots, wind and ember readouts, and a scrolling event log.

### Remote web dashboard
Available at **https://control.pyregon.ai** (see Infrastructure below).

Mirrors all local panel state in real time via WebSocket. Supports the same controls: engine start/stop, zone toggles, mode toggle, and emergency all-off.

---

## Infrastructure

```
Browser (anywhere)
      │  HTTPS
      ▼
Cloudflare Edge
      │  Cloudflare Access — email PIN authentication
      │  (only sebastien.cayolle@gmail.com and librodo112@gmail.com allowed)
      ▼
cloudflared tunnel (systemd service on Pi)
      │  encrypted tunnel — no open inbound ports required
      ▼
localhost:8080 on Pi
      │
      ▼
web_panel.py (FastAPI + WebSocket, daemon thread inside control_panel.py)
```

### Cloudflare Tunnel
- Tunnel name: `pyregon`
- Tunnel ID: `302fd4f9-f2f2-4dae-b6b6-fe7a9612c0db`
- Config: `/etc/cloudflared/config.yml`
- Credentials: `/home/librodo112/.cloudflared/302fd4f9-f2f2-4dae-b6b6-fe7a9612c0db.json`
- Runs as a systemd service (`cloudflared.service`), starts on boot
- No inbound firewall ports needed — the Pi initiates an outbound tunnel to Cloudflare

### Cloudflare Access (authentication)
- Zero Trust application: `control.pyregon.ai`
- Policy: **Allow** — Emails: `sebastien.cayolle@gmail.com`, `librodo112@gmail.com`
- Unauthenticated visitors are redirected to a Cloudflare login page and receive a one-time PIN by email

### DNS
- `pyregon.ai` → Squarespace (public marketing site)
- `control.pyregon.ai` → Cloudflare Tunnel → Pi `:8080`
- DNS managed by Cloudflare; domain registered via Squarespace

---

## Running on the Pi

```bash
# Start the full system (touchscreen + web panel + pump)
python3 /home/librodo112/frigate_rpi/control_panel.py

# SSH access
ssh librodo112@192.168.1.153

# VNC access
# See: https://www.raspberrypi.com/documentation/computers/remote-access.html#connect-to-a-vnc-server

# Pi configuration
sudo raspi-config    # SSH, VNC, display settings
sudo nmtui           # Network / static IP
```

## Docker containers

```bash
docker ps   # shows Frigate (Hailo) and local Roboflow inference server

# Roboflow model preload
curl -X POST http://localhost:9001/model/add \
  -H "Content-Type: application/json" \
  -d '{"model_id":"ember-training-poc/1","api_key":"'"$ROBOFLOW_API_KEY"'"}'
```

## Key files

| File | Purpose |
|------|---------|
| `control_panel.py` | Main process — Tkinter UI, spawns pump.py, owns relays |
| `pump.py` | Frigate → Roboflow inference loop (multi-camera) |
| `auto_mode.py` | AUTO mode state machine |
| `anemometer.py` | Modbus RTU driver + SimulatedAnemometer fallback |
| `web_panel.py` | FastAPI + WebSocket remote dashboard server |
| `static/index.html` | Web dashboard frontend |
| `config.py` | Tunable parameters (thresholds, durations, zone GPS coords) |
| `pyregon_config.json` | Runtime config overrides (persisted across restarts) |
| `pyregon_events.db` | SQLite event log (last 500 events shown in web dashboard) |
