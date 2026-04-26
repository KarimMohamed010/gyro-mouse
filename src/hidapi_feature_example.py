#!/usr/bin/env python3
"""
Minimal Windows hidapi example for the ESP32 MPU composite BLE HID device.

Pair the ESP32 in Windows Bluetooth settings first. If the HID descriptor
changed, remove the old device from Bluetooth settings and pair again.
"""

import time
import hid

VID = 0xE502
PID = 0xA111
REPORT_ID_FEATURE = 3
REPORT_ID_GESTURE = 2
PAGE_SELECT = 0x7F


def open_esp32():
    for item in hid.enumerate(VID, PID):
        product = item.get("product_string") or ""
        if "ESP32 MPU Mouse" in product:
            dev = hid.device()
            dev.open_path(item["path"])
            return dev
    dev = hid.device()
    dev.open(VID, PID)
    return dev


def set_feature(dev, payload8):
    if len(payload8) != 8:
        raise ValueError("feature payload must be exactly 8 bytes")
    # Windows HID APIs use byte 0 as the report ID, then the 8-byte payload.
    dev.send_feature_report(bytes([REPORT_ID_FEATURE] + list(payload8)))


def get_feature(dev, page):
    set_feature(dev, [PAGE_SELECT, page, 0, 0, 0, 0, 0, 0])
    time.sleep(0.05)
    report = list(dev.get_feature_report(REPORT_ID_FEATURE, 9))
    if len(report) >= 9 and report[0] == REPORT_ID_FEATURE:
        return report[1:9]
    return report[:8]


def main():
    dev = open_esp32()
    dev.set_nonblocking(True)

    # Example: write page 1 gains. payload = [page, gainX u16, gainY u16, 0,0,0]
    set_feature(dev, [1, 30, 0, 30, 0, 0, 0, 0])
    print("Page 1:", get_feature(dev, 1))

    print("Waiting for gesture input reports...")
    while True:
        report = dev.read(64)
        if report and report[0] == REPORT_ID_GESTURE:
            print("gesture_id=", report[1], "gesture_data=", report[2])
        time.sleep(0.02)


if __name__ == "__main__":
    main()
