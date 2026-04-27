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
    path_feature = None
    for item in hid.enumerate():
        product = item.get("product_string") or ""
        if "ESP32 MPU Mouse" in product:
            if item.get("usage_page") == 65280 and item.get("usage") == 16:
                path_feature = item["path"]
            elif not path_feature:
                path_feature = item["path"]
                
    if path_feature:
        if hasattr(hid, 'Device'):
            dev = hid.Device(path=path_feature)
            dev.nonblocking = True
            return dev
        else:
            dev = hid.device()
            dev.open_path(path_feature)
            dev.set_nonblocking(True)
            return dev
    raise RuntimeError("ESP32 MPU Mouse not found")


def set_feature(dev, payload8):
    if len(payload8) != 8:
        raise ValueError("feature payload must be exactly 8 bytes")
    # Windows HID APIs use byte 0 as the report ID, then the 8-byte payload.
    if hasattr(dev, 'send_feature_report'):
        dev.send_feature_report(bytes([REPORT_ID_FEATURE] + list(payload8)))
    else:
        dev.send_feature_report([REPORT_ID_FEATURE] + list(payload8))


def get_feature(dev, page):
    set_feature(dev, [PAGE_SELECT, page, 0, 0, 0, 0, 0, 0])
    time.sleep(0.05)
    report = list(dev.get_feature_report(REPORT_ID_FEATURE, 9))
    if len(report) >= 9 and report[0] == REPORT_ID_FEATURE:
        return report[1:9]
    return report[:8]


def main():
    dev = open_esp32()

    # Example: write page 1 gains. payload = [page, gainX u16, gainY u16, 0,0,0]
    set_feature(dev, [1, 30, 0, 30, 0, 0, 0, 0])
    print("Page 1:", get_feature(dev, 1))

    print("Note: To read gestures, open the collection with usage_page 65280, usage 1.")
    dev.close()

if __name__ == "__main__":
    main()
