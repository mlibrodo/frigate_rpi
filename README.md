# frigate_rpi

passwords and ROBOFLOW_API_KEY in (google doc)[https://docs.google.com/document/d/1Ijb5Ih6niiNby-Oxc1I2EeX49zCnluoEAXFwS41Kqhk/edit?usp=sharing)

Network Configuration from mikes home flipedonce
192.168.1.153

it has a static IP `ssh librodo112@192.168.1.153`

you can also VNC using remote desktop so you can use a VNC client. Follow instructions from [here](https://www.raspberrypi.com/documentation/computers/remote-access.html#connect-to-a-vnc-server)

some useful RPI terminal commands
```
sudo raspi-config  # configure SSH, VNC setup, etc
sudo nmtui         # configure network, static ip
```

Some images and running containers in th RPI
```
librodo112@pi-hailo:~/frigate_rpi $ docker images
                                                                                               i Info →   U  In Use
IMAGE                                               ID             DISK USAGE   CONTENT SIZE   EXTRA
ghcr.io/blakeblackshear/frigate:stable              1f8dbaaa4c7c       3.37GB          866MB        
ghcr.io/blakeblackshear/frigate:stable-h8l          b2bea5a2a9ae       2.57GB          661MB    U   
ghcr.io/blakeblackshear/frigate:stable-synaptics    856a4474a841       6.01GB         1.38GB        
python:3.11-slim                                    d6e4d224f70f        214MB         47.9MB    U   
roboflow/roboflow-inference-server-arm-cpu:latest   0b63419ea4d2       3.39GB          897MB    U   
librodo112@pi-hailo:~/frigate_rpi $ docker ps
CONTAINER ID   IMAGE              COMMAND                  CREATED       STATUS          PORTS                                         NAMES
3f12dbd95c13   python:3.11-slim   "sh -c 'pip install …"   2 weeks ago   Up 24 minutes   0.0.0.0:9002->9002/tcp, [::]:9002->9002/tcp   roboflow-bridge
librodo112@pi-hailo:~/frigate_rpi $ uname -a
Linux pi-hailo 6.12.62+rpt-rpi-2712 #1 SMP PREEMPT Debian 1:6.12.62-1+rpt1~bookworm (2026-01-19) aarch64 GNU/Linux

```


```
curl -X POST http://localhost:9001/model/add \
  -H "Content-Type: application/json" \
  -d '{"model_id":"ember-training-poc/1","api_key":"'"$ROBOFLOW_API_KEY"'"}'
```

```
python3 pump.py
```
