# MPU Serial Recorder

This recorder captures ESP32 MPU6050 serial data with labels.

## Expected ESP32 stream format

Each sample line from firmware must be:

`ax,ay,az,gx,gy,gz,yaw,pitch,roll`

The updated firmware in `main.cpp` now emits this format at about 50 Hz.

## Install

```bash
pip install -r requirements-recorder.txt
```

## Run

```bash
python mpu_serial_recorder.py --port COM5 --baud 115200 --out recordings
```

If `--port` is omitted, the first detected serial port is used.

## Controls

- `1` = swipe
- `2` = shake
- `3` = circle
- `4` = wave
- `5` = idle
- `space` = start/stop recording
- `q` = quit

Each recording is saved as a separate CSV with rows:

`timestamp,ax,ay,az,gx,gy,gz,yaw,pitch,roll,label`

## Optional live plot

```bash
python mpu_serial_recorder.py --port COM5 --plot
```
