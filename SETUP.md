# Pyregon — Fresh Pi Setup Guide

Complete reproduction guide for the Pyregon wildfire defense controller.

## Hardware

| Component | Model |
|---|---|
| Single-board computer | Raspberry Pi 5 |
| AI accelerator | Hailo-8L (M.2) |
| Relay HAT | Sequent Microsystems 8relind (8-relay, board 0) |
| Relay/input HAT | Sequent Microsystems 4rel4in (4-relay/4-input, stack level 1) |
| Anemometer | Renke RS-CFSFX-N01-3H-EX (RS-485 Modbus RTU, 4800 baud, addr 1) |
| LTE dongle | Quectel EC25-AF (North America bands) |
| Display | 800×480 touchscreen (HDMI) |

## 1. Operating System

Raspberry Pi OS 64-bit **Bookworm** (Debian 12).

## 2. Boot configuration

### `/boot/firmware/config.txt` — add to `[all]` section:
```
dtparam=i2c_arm=on
dtoverlay=i2c1
dtoverlay=uart0-pi5
```
> `uart0-pi5` is required on Pi 5 to expose UART0 on GPIO 14/15 for the RS-485 anemometer.
> Without it `/dev/ttyAMA0` does not exist.

### `/boot/firmware/cmdline.txt` — remove this token:
```
console=serial0,115200
```

### Disable serial getty:
```bash
sudo systemctl disable serial-getty@ttyAMA0.service
```

## 3. System packages

```bash
sudo apt update && sudo apt install -y \
  docker-ce docker-ce-cli docker-compose-plugin \
  modemmanager network-manager \
  usb-modeswitch usb-modeswitch-data \
  libqmi-utils \
  i2c-tools \
  python3-pip \
  git
```

Docker CE requires the official Docker apt repo — follow docker.com/install/linux/docker-ce/debian.

## 4. Hailo drivers

Install via Hailo's Raspberry Pi 5 installer. Versions deployed:

| Package | Version |
|---|---|
| hailo-dkms | 4.19.0 |
| hailofw | 4.19.0 |
| hailort | 4.19.0 |
| hailo-tappas-core | 3.31.0 |
| python3-hailort | 4.19.0 |

## 5. Sequent Microsystems HAT drivers

Build and install from source:

```bash
git clone https://github.com/SequentMicrosystems/4rel4in-rpi ~/4rel4in-rpi
cd ~/4rel4in-rpi && sudo make install

git clone https://github.com/SequentMicrosystems/8relind-rpi ~/8relind-rpi
cd ~/8relind-rpi && sudo make install
```

### 4rel4in HAT configuration (one-time, survives reboot via HAT EEPROM):
```bash
# Disable HAT onboard Modbus — releases RS-485 bus to Pi UART
4rel4in 1 cfg485wr 0

# Verify:
4rel4in 1 cfg485rd   # should show mode=0
```

**DIP switches on the HAT:**
- Stack level: **1**
- 485-TX: **ON**
- 485-RX: **ON**
- 485-TERM: **ON** (anemometer is only device on bus)

## 6. Python packages

```bash
pip3 install \
  fastapi==0.136.1 \
  uvicorn==0.46.0 \
  minimalmodbus==2.1.1 \
  smbus2==0.4.2 \
  requests==2.32.5 \
  pillow==11.3.0 \
  pydantic==2.13.3 \
  roboflow==1.2.16
```

## 7. Cloudflare tunnel

```bash
# Install cloudflared (see cloudflare.com/products/tunnel)
sudo cloudflared service install <tunnel-token>
```

Tunnel routes `control.pyregon.ai` → `http://127.0.0.1:8080`.
Cloudflare Access (Zero Trust) gates it with email PIN auth.
Authorized: sebastien.cayolle@gmail.com, librodo112@gmail.com.

## 8. Project repo

```bash
git clone https://github.com/mlibrodo/frigate_rpi ~/frigate_rpi
```

### Create `~/frigate_rpi/.env`:
```
ROBOFLOW_API_KEY=<key from Roboflow dashboard>
```

## 9. Docker containers

```bash
cd ~/frigate_rpi
docker compose up -d
```

Starts:
- **frigate** — NVR at `http://localhost:5000`, uses Hailo-8L via `/dev/hailo0`
- **inference-server** — Roboflow local inference at `http://localhost:9001`

### roboflow-bridge (not yet in docker-compose — see open issue):
```bash
docker run -d --name roboflow-bridge --restart unless-stopped \
  -p 9002:9002 \
  -v /opt/frigate/roboflow-bridge:/app \
  -e ROBOFLOW_API_KEY=<key> \
  python:3.11-slim \
  sh -c "pip install requests -q && python /app/bridge.py"
```

## 10. Anemometer wiring

| Wire | Connection |
|---|---|
| Brown | 12V+ (power supply positive) |
| Black | 12V GND (power supply negative) |
| Yellow | RS-485 A terminal on 4rel4in HAT |
| Blue | RS-485 B terminal on 4rel4in HAT |

Serial config: `/dev/ttyAMA0`, 4800 baud, addr 1, 8N1.

## 11. systemd services

`pump.service` is present but **disabled** — `control_panel.py` manages `pump.py` directly.

```bash
sudo cp ~/frigate_rpi/pump.service /etc/systemd/system/
sudo systemctl daemon-reload
# Do NOT enable — leave disabled
```

## 12. Start the application

```bash
cd ~/frigate_rpi && DISPLAY=:0 python3 control_panel.py
```

The app starts the Tkinter touchscreen UI and launches the web panel on port 8080.
`control.pyregon.ai` is live once cloudflared is running.

## Running services summary

| Service | How it runs | Port |
|---|---|---|
| Frigate | Docker (always) | 5000 |
| inference-server | Docker (always) | 9001 |
| roboflow-bridge | Docker (always) | 9002 |
| cloudflared | systemd (always) | — |
| control_panel.py | Manual / autostart | 8080 |
| pump.py | Subprocess of control_panel.py | — |
