#include <Arduino.h>
#include "I2Cdev.h"
#include "MPU6050_6Axis_MotionApps20.h"
#include <Wire.h>
#include <BLEDevice.h>
#include <BLEServer.h>
#include <BLEUtils.h>
#include <BLEHIDDevice.h>
#include <HIDTypes.h>
#include <Preferences.h>

// ─── Hardware ─────────────────────────────────────────────────────────────────
constexpr uint8_t INTERRUPT_PIN = 2;

// ─── HID report IDs ───────────────────────────────────────────────────────────
constexpr uint8_t REPORT_ID_MOUSE = 1;
constexpr uint8_t REPORT_ID_GESTURE = 2;
constexpr uint8_t REPORT_ID_FEATURE = 3;
constexpr uint8_t FEATURE_PAYLOAD_SIZE = 8;

// ─── Timing ───────────────────────────────────────────────────────────────────
constexpr uint16_t BLE_POST_CONNECT_DELAY_MS = 1200;
constexpr uint16_t BLE_REPORT_INTERVAL_MS = 16;
constexpr uint16_t GESTURE_COOLDOWN_MS = 800;
constexpr uint16_t SHAKE_WINDOW_MS = 800;
constexpr uint16_t DOUBLE_TILT_WINDOW_MS = 500;
constexpr uint8_t SHAKE_REVERSALS_REQUIRED = 4;
constexpr uint8_t CIRCLE_FRAMES_REQUIRED = 12;
constexpr uint16_t SAMPLE_INTERVAL_MS = 20; // 50 Hz

// ─── Drift correction ─────────────────────────────────────────────────────────
// When all rv[] components are below DRIFT_STILL_THRESHOLD the device is
// considered stationary and q_ref is slowly slerped toward q at DRIFT_RATE.
// Result: gyro bias drift is cancelled automatically without any jump.
//
// DRIFT_STILL_THRESHOLD — half-angle units (~6°). Must be larger than your
//   deadzones but smaller than the smallest intentional movement you want to
//   register.  Tune downward if correction triggers during slow intentional
//   movement; tune upward if drift isn't corrected fast enough.
//
// DRIFT_RATE — fraction of remaining error removed per second (0..1).
//   0.5 = half the error gone each second.  Keep below 1.0.
//   Too high → feels like the cursor is being pulled back to centre.
//   Too low  → drift wins.
constexpr float DRIFT_STILL_THRESHOLD = 0.18f; // ~6°
constexpr float DRIFT_RATE = 2.0f;             // per second

// ─── Runtime gyro bias estimator ──────────────────────────────────────────────
// While the device is still we accumulate a low-pass average of the raw gyro
// readings. Once BIAS_MIN_STILL_FRAMES consecutive still frames have passed,
// the negated mean is written to the MPU6050 hardware offset registers.
// This corrects the source of drift rather than just cleaning up after it.
//
// BIAS_LOWPASS_ALPHA — smoothing factor (0..1). Smaller = slower but more
//   stable estimate. 0.01 gives ~2 s settling at 50 Hz.
// BIAS_MIN_STILL_FRAMES — how many consecutive still frames before we commit.
//   50 frames = 1 second at 50 Hz. Prevents committing during brief pauses.
// BIAS_APPLY_THRESHOLD_LSB — only rewrite registers if the estimated bias
//   has changed by more than this many LSBs. Prevents thrashing the I²C bus.
constexpr float BIAS_LOWPASS_ALPHA = 0.05f;
constexpr uint16_t BIAS_MIN_STILL_FRAMES = 20; // 1 second
constexpr int16_t BIAS_APPLY_THRESHOLD_LSB = 2;

// ─── Rotation-vector axis indices (vx=0, vy=1, vz=2) ─────────────────────────
// These replace the old AXIS_YAW / AXIS_PITCH / AXIS_ROLL constants.
// The meaning is now purely geometric: rotation around the device's X, Y, Z
// axes measured relative to the reference quaternion.
constexpr uint8_t AXIS_X = 0; // roll-like (side tilt)
constexpr uint8_t AXIS_Y = 1; // pitch-like (forward/back tilt)
constexpr uint8_t AXIS_Z = 2; // yaw-like   (in-plane twist)

// ─── Gesture IDs ─────────────────────────────────────────────────────────────
constexpr uint8_t GESTURE_FLICK = 1;
constexpr uint8_t GESTURE_SHAKE = 2;
constexpr uint8_t GESTURE_DOUBLE_TILT = 3;
constexpr uint8_t GESTURE_CIRCLE = 4;
constexpr uint8_t GESTURE_AXIS_NONE = 3;
constexpr uint8_t GESTURE_DIRECTION_POS = 0x80;

// ─── Feature page IDs ─────────────────────────────────────────────────────────
constexpr uint8_t FEATURE_PAGE_BASIC = 0;
constexpr uint8_t FEATURE_PAGE_GAINS = 1;
constexpr uint8_t FEATURE_PAGE_FLICK = 2;
constexpr uint8_t FEATURE_PAGE_OTHER_GESTURES = 3;
constexpr uint8_t FEATURE_PAGE_COMMAND = 0x7E;
constexpr uint8_t FEATURE_PAGE_SELECT = 0x7F;

// ─────────────────────────────────────────────────────────────────────────────
// Config
// ─────────────────────────────────────────────────────────────────────────────
struct Config
{
  // cursorXAxis / cursorYAxis: which position-proxy component drives the
  // cursor horizontally / vertically.  clickAxis: drives tilt-click.
  uint8_t cursorXAxis = AXIS_Z; // yaw-like → left/right
  uint8_t cursorYAxis = AXIS_Y; // pitch-like → up/down
  uint8_t clickAxis = AXIS_X;   // roll-like → tilt click

  bool invertX = false;
  bool invertY = false;
  bool invertClick = false;

  // All thresholds are in "half-angle" units: 2·sin(θ/2).
  // Quick conversions: 10° ≈ 0.174, 15° ≈ 0.261, 25° ≈ 0.431, 30° ≈ 0.518
  float deadzoneX = 0.026f; // ~1.5°
  float deadzoneY = 0.026f;
  float deadzoneClick = 0.035f;

  float gainX = 150.f; // pixels per half-angle unit  (tune to taste)
  float gainY = 150.f;

  float tiltThreshRad = 0.518f; // ~30°

  // Velocity thresholds are in half-angle units per second.
  float flickVelThresh = 3.5f;  // ~200°/s
  float flickReturnRad = 0.14f; // ~8°
  uint16_t flickConfirmMs = 300;
  float shakeVelThresh = 1.74f; // ~100°/s
  float doubleTiltRad = 0.431f; // ~25°
  float circleMinSpeed = 0.52f; // ~30°/s

  bool enableFlick = true;
  bool enableShake = true;
  bool enableDoubleTilt = true;
  bool enableCircle = true;
  bool enableClicks = true;
};

Config cfg;
Preferences prefs;
volatile bool requestRecenter = false;

// ─────────────────────────────────────────────────────────────────────────────
// Utility
// ─────────────────────────────────────────────────────────────────────────────
inline int8_t signOf(float v) { return (v > 0.f) ? 1 : (v < 0.f) ? -1
                                                                 : 0; }

inline float applyDeadband(float v, float db)
{
  return fabsf(v) < db ? 0.f : v;
}

uint8_t clampU8(float value, float scale)
{
  long v = lroundf(value * scale);
  if (v < 0)
    return 0;
  if (v > 255)
    return 255;
  return static_cast<uint8_t>(v);
}
uint16_t clampU16(float value, float scale)
{
  long v = lroundf(value * scale);
  if (v < 0)
    return 0;
  if (v > 65535)
    return 65535;
  return static_cast<uint16_t>(v);
}
void writeU16LE(uint8_t *dst, uint16_t value)
{
  dst[0] = static_cast<uint8_t>(value & 0xFF);
  dst[1] = static_cast<uint8_t>((value >> 8) & 0xFF);
}
uint16_t readU16LE(const uint8_t *src)
{
  return static_cast<uint16_t>(src[0]) | (static_cast<uint16_t>(src[1]) << 8);
}
uint8_t sanitizeAxis(uint8_t axis, uint8_t fallback)
{
  return (axis <= AXIS_Z) ? axis : fallback;
}

void sanitizeConfig()
{
  cfg.cursorXAxis = sanitizeAxis(cfg.cursorXAxis, AXIS_Z);
  cfg.cursorYAxis = sanitizeAxis(cfg.cursorYAxis, AXIS_Y);
  cfg.clickAxis = sanitizeAxis(cfg.clickAxis, AXIS_X);
  cfg.deadzoneX = constrain(cfg.deadzoneX, 0.f, 2.55f);
  cfg.deadzoneY = constrain(cfg.deadzoneY, 0.f, 2.55f);
  cfg.deadzoneClick = constrain(cfg.deadzoneClick, 0.f, 2.55f);
  cfg.gainX = constrain(cfg.gainX, 0.f, 655.35f);
  cfg.gainY = constrain(cfg.gainY, 0.f, 655.35f);
  cfg.tiltThreshRad = constrain(cfg.tiltThreshRad, 0.f, 1.5708f);
  cfg.flickVelThresh = constrain(cfg.flickVelThresh, 0.f, 655.35f);
  cfg.flickReturnRad = constrain(cfg.flickReturnRad, 0.f, 2.55f);
  cfg.flickConfirmMs = (uint16_t)constrain((int)cfg.flickConfirmMs, 0, 65535);
  cfg.shakeVelThresh = constrain(cfg.shakeVelThresh, 0.f, 655.35f);
  cfg.doubleTiltRad = constrain(cfg.doubleTiltRad, 0.f, 2.55f);
  cfg.circleMinSpeed = constrain(cfg.circleMinSpeed, 0.f, 655.35f);
}

// Increment this any time Config fields change meaning, are added, or removed.
// A mismatch with the stored value causes a clean wipe and default reload.
constexpr uint8_t CONFIG_VERSION = 2; // quaternion rewrite: radian units

void saveConfig()
{
  sanitizeConfig();
  prefs.begin("mpu", false);
  prefs.putUChar("ver", CONFIG_VERSION);
  prefs.putBytes("cfg", &cfg, sizeof(cfg));
  prefs.end();
}

void loadConfig()
{
  cfg = Config{}; // always start from defaults

  prefs.begin("mpu", true);
  const uint8_t storedVer = prefs.getUChar("ver", 0xFF);
  const bool versionMatch = (storedVer == CONFIG_VERSION);
  if (versionMatch)
    prefs.getBytes("cfg", &cfg, sizeof(cfg));
  prefs.end();

  if (!versionMatch)
  {
    // Erase everything in this namespace so next saveConfig() starts clean
    prefs.begin("mpu", false);
    prefs.clear();
    prefs.end();
    Serial.printf("CONFIG: version mismatch (stored=%u expected=%u) — using defaults\n",
                  storedVer, CONFIG_VERSION);
  }

  sanitizeConfig();
}

// ─────────────────────────────────────────────────────────────────────────────
// HID report descriptor  (unchanged from original)
// ─────────────────────────────────────────────────────────────────────────────
static const uint8_t hidReportDescriptor[] = {
    0x05, 0x01, 0x09, 0x02, 0xA1, 0x01,
    0x85, REPORT_ID_MOUSE,
    0x09, 0x01, 0xA1, 0x00,
    0x05, 0x09, 0x19, 0x01, 0x29, 0x08,
    0x15, 0x00, 0x25, 0x01, 0x95, 0x08, 0x75, 0x01, 0x81, 0x02,
    0x05, 0x01, 0x09, 0x30, 0x09, 0x31, 0x09, 0x38,
    0x15, 0x81, 0x25, 0x7F, 0x75, 0x08, 0x95, 0x03, 0x81, 0x06,
    0xC0, 0xC0,

    0x06, 0x00, 0xFF, 0x09, 0x01, 0xA1, 0x01,
    0x85, REPORT_ID_GESTURE,
    0x09, 0x02, 0x15, 0x00, 0x26, 0xFF, 0x00, 0x75, 0x08, 0x95, 0x02, 0x81, 0x02,
    0xC0,

    0x06, 0x00, 0xFF, 0x09, 0x10, 0xA1, 0x01,
    0x85, REPORT_ID_FEATURE,
    0x09, 0x11, 0x15, 0x00, 0x26, 0xFF, 0x00, 0x75, 0x08,
    0x95, FEATURE_PAYLOAD_SIZE, 0xB1, 0x02,
    0xC0};

// ─────────────────────────────────────────────────────────────────────────────
// BLE HID globals
// ─────────────────────────────────────────────────────────────────────────────
BLEHIDDevice *hid = nullptr;
BLECharacteristic *mouseInput = nullptr;
BLECharacteristic *gestureInput = nullptr;
BLECharacteristic *featureReport = nullptr;
bool hidConnected = false;
uint32_t hidConnectedSinceMs = 0;
uint8_t currentFeaturePage = FEATURE_PAGE_BASIC;

void enableInputNotifications(BLECharacteristic *input)
{
  if (!input)
    return;
  BLE2902 *cccd = static_cast<BLE2902 *>(
      input->getDescriptorByUUID(BLEUUID((uint16_t)0x2902)));
  if (cccd)
    cccd->setNotifications(true);
}

void enableHidInputNotifications()
{
  enableInputNotifications(mouseInput);
  enableInputNotifications(gestureInput);
}

// ─────────────────────────────────────────────────────────────────────────────
// Feature report encode / decode
// NOTE: Deadzone and threshold values are now in units of 0.01 rad (×100)
//       Gain values are unchanged (×100).
//       Velocity thresholds are in 0.01 rad/s (×100).
// ─────────────────────────────────────────────────────────────────────────────
void buildFeaturePayload(uint8_t page, uint8_t out[FEATURE_PAYLOAD_SIZE])
{
  memset(out, 0, FEATURE_PAYLOAD_SIZE);
  out[0] = page;
  switch (page)
  {
  case FEATURE_PAGE_BASIC:
    out[1] = ((cfg.cursorXAxis & 0x03) |
              ((cfg.cursorYAxis & 0x03) << 2) |
              ((cfg.clickAxis & 0x03) << 4));
    out[2] = (cfg.invertX ? 0x01 : 0) |
             (cfg.invertY ? 0x02 : 0) |
             (cfg.invertClick ? 0x04 : 0) |
             (cfg.enableClicks ? 0x08 : 0) |
             (cfg.enableFlick ? 0x10 : 0) |
             (cfg.enableShake ? 0x20 : 0) |
             (cfg.enableDoubleTilt ? 0x40 : 0) |
             (cfg.enableCircle ? 0x80 : 0);
    out[3] = clampU8(cfg.deadzoneX, 100.f);
    out[4] = clampU8(cfg.deadzoneY, 100.f);
    out[5] = clampU8(cfg.deadzoneClick, 100.f);
    out[6] = clampU8(cfg.tiltThreshRad, 100.f);
    break;
  case FEATURE_PAGE_GAINS:
    writeU16LE(&out[1], clampU16(cfg.gainX, 100.f));
    writeU16LE(&out[3], clampU16(cfg.gainY, 100.f));
    break;
  case FEATURE_PAGE_FLICK:
    writeU16LE(&out[1], clampU16(cfg.flickVelThresh, 100.f));
    out[3] = clampU8(cfg.flickReturnRad, 100.f);
    writeU16LE(&out[4], cfg.flickConfirmMs);
    break;
  case FEATURE_PAGE_OTHER_GESTURES:
    writeU16LE(&out[1], clampU16(cfg.shakeVelThresh, 100.f));
    out[3] = clampU8(cfg.doubleTiltRad, 100.f);
    writeU16LE(&out[4], clampU16(cfg.circleMinSpeed, 100.f));
    break;
  default:
    out[0] = currentFeaturePage;
    break;
  }
}

void refreshFeatureCharacteristic()
{
  if (!featureReport)
    return;
  uint8_t payload[FEATURE_PAYLOAD_SIZE];
  buildFeaturePayload(currentFeaturePage, payload);
  featureReport->setValue(payload, FEATURE_PAYLOAD_SIZE);
}

void applyFeaturePayload(const uint8_t data[FEATURE_PAYLOAD_SIZE])
{
  const uint8_t page = data[0];
  if (page == FEATURE_PAGE_COMMAND)
  {
    if (data[1] == 1)
      requestRecenter = true;
    return;
  }
  if (page == FEATURE_PAGE_SELECT)
  {
    if (data[1] <= FEATURE_PAGE_OTHER_GESTURES)
      currentFeaturePage = data[1];
    refreshFeatureCharacteristic();
    return;
  }
  switch (page)
  {
  case FEATURE_PAGE_BASIC:
    cfg.cursorXAxis = sanitizeAxis(data[1] & 0x03, AXIS_Z);
    cfg.cursorYAxis = sanitizeAxis((data[1] >> 2) & 0x03, AXIS_Y);
    cfg.clickAxis = sanitizeAxis((data[1] >> 4) & 0x03, AXIS_X);
    cfg.invertX = (data[2] & 0x01) != 0;
    cfg.invertY = (data[2] & 0x02) != 0;
    cfg.invertClick = (data[2] & 0x04) != 0;
    cfg.enableClicks = (data[2] & 0x08) != 0;
    cfg.enableFlick = (data[2] & 0x10) != 0;
    cfg.enableShake = (data[2] & 0x20) != 0;
    cfg.enableDoubleTilt = (data[2] & 0x40) != 0;
    cfg.enableCircle = (data[2] & 0x80) != 0;
    cfg.deadzoneX = data[3] / 100.f;
    cfg.deadzoneY = data[4] / 100.f;
    cfg.deadzoneClick = data[5] / 100.f;
    cfg.tiltThreshRad = data[6] / 100.f;
    break;
  case FEATURE_PAGE_GAINS:
    cfg.gainX = readU16LE(&data[1]) / 100.f;
    cfg.gainY = readU16LE(&data[3]) / 100.f;
    break;
  case FEATURE_PAGE_FLICK:
    cfg.flickVelThresh = readU16LE(&data[1]) / 100.f;
    cfg.flickReturnRad = data[3] / 100.f;
    cfg.flickConfirmMs = readU16LE(&data[4]);
    break;
  case FEATURE_PAGE_OTHER_GESTURES:
    cfg.shakeVelThresh = readU16LE(&data[1]) / 100.f;
    cfg.doubleTiltRad = data[3] / 100.f;
    cfg.circleMinSpeed = readU16LE(&data[4]) / 100.f;
    break;
  default:
    return;
  }
  currentFeaturePage = page;
  saveConfig();
  refreshFeatureCharacteristic();
}

// ─────────────────────────────────────────────────────────────────────────────
// BLE callbacks
// ─────────────────────────────────────────────────────────────────────────────
class FeatureCallbacks : public BLECharacteristicCallbacks
{
  void onRead(BLECharacteristic *c, esp_ble_gatts_cb_param_t *) override
  {
    (void)c;
    refreshFeatureCharacteristic();
  }
  void onWrite(BLECharacteristic *c, esp_ble_gatts_cb_param_t *) override
  {
    std::string v = c->getValue();
    if (v.size() == FEATURE_PAYLOAD_SIZE)
      applyFeaturePayload(reinterpret_cast<const uint8_t *>(v.data()));
  }
};

class HidServerCallbacks : public BLEServerCallbacks
{
  void onConnect(BLEServer *) override
  {
    hidConnected = true;
    hidConnectedSinceMs = millis();
    enableHidInputNotifications();
    requestRecenter = true;
  }
  void onDisconnect(BLEServer *server) override
  {
    hidConnected = false;
    hidConnectedSinceMs = 0;
    server->getAdvertising()->start();
  }
};

// ─────────────────────────────────────────────────────────────────────────────
// Report sending
// ─────────────────────────────────────────────────────────────────────────────
bool hidReportsReady(uint32_t nowMs)
{
  return hidConnected &&
         ((nowMs - hidConnectedSinceMs) >= BLE_POST_CONNECT_DELAY_MS);
}

void sendMouse(int8_t x, int8_t y, uint8_t buttons = 0)
{
  if (!mouseInput || !hidReportsReady(millis()))
    return;
  uint8_t report[4] = {buttons,
                       static_cast<uint8_t>(x),
                       static_cast<uint8_t>(y), 0};
  mouseInput->setValue(report, sizeof(report));
  mouseInput->notify();
}

void sendGesture(uint8_t id, uint8_t data)
{
  if (!gestureInput || !hidReportsReady(millis()))
    return;
  uint8_t report[2] = {id, data};
  gestureInput->setValue(report, sizeof(report));
  gestureInput->notify();
}

uint8_t encodeGestureData(uint8_t axis, int8_t direction)
{
  uint8_t d = (axis <= AXIS_Z) ? axis : GESTURE_AXIS_NONE;
  if (direction > 0)
    d |= GESTURE_DIRECTION_POS;
  return d;
}

// ─────────────────────────────────────────────────────────────────────────────
// BLE HID setup
// ─────────────────────────────────────────────────────────────────────────────
void setupHID()
{
  BLEDevice::init("ESP32 MPU Mouse");
  BLEServer *server = BLEDevice::createServer();
  server->setCallbacks(new HidServerCallbacks());

  hid = new BLEHIDDevice(server);
  mouseInput = hid->inputReport(REPORT_ID_MOUSE);
  gestureInput = hid->inputReport(REPORT_ID_GESTURE);
  featureReport = hid->featureReport(REPORT_ID_FEATURE);
  enableHidInputNotifications();
  featureReport->setCallbacks(new FeatureCallbacks());

  hid->manufacturer()->setValue("Espressif");
  hid->pnp(0x02, 0xE502, 0xA111, 0x0210);
  hid->hidInfo(0x00, 0x02);
  hid->reportMap(const_cast<uint8_t *>(hidReportDescriptor),
                 sizeof(hidReportDescriptor));
  refreshFeatureCharacteristic();
  hid->startServices();
  hid->setBatteryLevel(100);

  BLESecurity *security = new BLESecurity();
  security->setAuthenticationMode(ESP_LE_AUTH_BOND);

  BLEAdvertising *advertising = server->getAdvertising();
  advertising->setAppearance(HID_MOUSE);
  advertising->addServiceUUID(hid->hidService()->getUUID());
  advertising->start();
}

// ─────────────────────────────────────────────────────────────────────────────
// Quaternion helpers
// ─────────────────────────────────────────────────────────────────────────────

// Multiply two quaternions:  result = a * b
// Uses the MPU6050 library's Quaternion type {w, x, y, z}.
Quaternion quatMul(const Quaternion &a, const Quaternion &b)
{
  return Quaternion(
      a.w * b.w - a.x * b.x - a.y * b.y - a.z * b.z,
      a.w * b.x + a.x * b.w + a.y * b.z - a.z * b.y,
      a.w * b.y - a.x * b.z + a.y * b.w + a.z * b.x,
      a.w * b.z + a.x * b.y - a.y * b.x + a.z * b.w);
}

// Conjugate (= inverse for unit quaternion)
Quaternion quatConj(const Quaternion &q)
{
  return Quaternion(q.w, -q.x, -q.y, -q.z);
}

// Extract a smooth, jitter-free position vector from a relative unit quaternion.
//
// WHY NOT axis-angle (acos-based):
//   When q_rel is near identity, w ≈ 1 and sin(angle/2) ≈ 0.  Dividing x/y/z
//   by that near-zero sin causes massive amplification of floating-point noise,
//   producing the "jerky / jumping" symptom at rest.
//
// WHAT WE USE INSTEAD — imaginary-part proxy:
//   For a unit quaternion q = [w, x, y, z],  x = sin(θ/2)·nx  etc.
//   For small angles (< ~60°, which covers all cursor use),
//       sin(θ/2) ≈ θ/2,  so  x ≈ (θ/2)·nx.
//   Using 2·x directly as the position measure is therefore proportional to
//   the rotation angle, monotonic, and — crucially — has no division and no
//   discontinuity anywhere near identity.
//
// The hemisphere-flip guard (w < 0 → negate) handles the case where DMP drift
// pushes the quaternion through the w=0 equator, which would otherwise flip
// all three signs simultaneously and look like a large instantaneous jump.
//
// Resulting range: each component spans [-2, +2] for rotations up to ±180°,
// but in practice never exceeds ±1 (±60°) during normal use.
// Thresholds and gains in Config are scaled accordingly (units: ~half-radians).
void quatToPos(const Quaternion &q, float pos[3])
{
  // Keep the quaternion in the w ≥ 0 hemisphere for sign continuity.
  const float sign = (q.w < 0.f) ? -1.f : 1.f;
  pos[0] = 2.f * q.x * sign;
  pos[1] = 2.f * q.y * sign;
  pos[2] = 2.f * q.z * sign;
}

// Spherical-linear interpolation between two unit quaternions.
// t=0 → returns a, t=1 → returns b.
// For the tiny per-frame steps used in drift correction, nlerp
// (normalised linear interpolation) is numerically identical to slerp
// and avoids the expensive acos + sin/sin division.
Quaternion quatNlerp(const Quaternion &a, const Quaternion &b, float t)
{
  // Ensure we take the short arc (dot < 0 → negate b)
  const float dot = a.w * b.w + a.x * b.x + a.y * b.y + a.z * b.z;
  const float s = (dot < 0.f) ? -1.f : 1.f;
  Quaternion r(
      a.w + t * (s * b.w - a.w),
      a.x + t * (s * b.x - a.x),
      a.y + t * (s * b.y - a.y),
      a.z + t * (s * b.z - a.z));
  // Renormalise
  float n = sqrtf(r.w * r.w + r.x * r.x + r.y * r.y + r.z * r.z);
  if (n > 1e-6f)
  {
    r.w /= n;
    r.x /= n;
    r.y /= n;
    r.z /= n;
  }
  return r;
}
// ─────────────────────────────────────────────────────────────────────────────
// rv[3]  : current rotation-vector relative to reference, radians
//           rv[AXIS_X], rv[AXIS_Y], rv[AXIS_Z]
// vel[3] : angular velocity of each rotation-vector component, rad/s

static float rv[3] = {};         // current relative position proxy
static float prevRv[3] = {};     // previous frame's proxy
static float vel[3] = {};        // angular velocity  (proxy-units/s)
static uint32_t prevFrameMs = 0; // timestamp of previous processed frame

// ─── Gyro bias estimator state ────────────────────────────────────────────────
// gyroSmooth[3]    : low-pass filtered raw gyro (X, Y, Z) in raw LSB units
// stillFrames      : consecutive frames the device has been below threshold
// appliedBias[3]   : last bias values written to MPU registers (avoid redundant writes)
static float gyroSmooth[3] = {};
static uint16_t stillFrames = 0;
static int16_t appliedBias[3] = {};

// ─────────────────────────────────────────────────────────────────────────────
// Gesture state
// ─────────────────────────────────────────────────────────────────────────────
struct GestureState
{
  uint32_t lastGestureMs = 0;

  struct FlickAxis
  {
    uint8_t phase = 0;
    int8_t direction = 0;
    float originRv = 0.f; // rv value at trigger
    uint32_t armedMs = 0;
  } flick[3];

  int8_t shakeSign[3] = {};
  uint8_t shakeCount[3] = {};
  uint32_t shakeStartMs = 0;

  uint32_t doubleTiltFirstMs[3][2] = {};
  bool doubleTiltArmed[3][2] = {
      {true, true}, {true, true}, {true, true}};

  uint8_t circleFrames = 0;
  int8_t circleSignA = 0;
  int8_t circleSignB = 0;
} gs;

// ─────────────────────────────────────────────────────────────────────────────
// Gesture detection — all inputs via rv[] and vel[]
// ─────────────────────────────────────────────────────────────────────────────
void detectGestures(uint32_t nowMs)
{
  if (prevFrameMs == 0)
  {
    return;
  }

  const bool onCooldown = (nowMs - gs.lastGestureMs) < GESTURE_COOLDOWN_MS;

  // ── Flick ──────────────────────────────────────────────────────────────
  if (cfg.enableFlick)
  {
    for (int i = 0; i < 3; i++)
    {
      auto &fa = gs.flick[i];
      if (fa.phase == 0)
      {
        if (fabsf(vel[i]) > cfg.flickVelThresh)
        {
          fa.phase = 1;
          fa.direction = signOf(vel[i]);
          fa.originRv = rv[i];
          fa.armedMs = nowMs;
        }
      }
      else
      {
        if (nowMs - fa.armedMs > cfg.flickConfirmMs)
        {
          fa.phase = 0;
        }
        else if (fabsf(rv[i] - fa.originRv) < cfg.flickReturnRad)
        {
          if (!onCooldown)
          {
            sendGesture(GESTURE_FLICK,
                        encodeGestureData(i, fa.direction));
            gs.lastGestureMs = nowMs;
          }
          fa.phase = 0;
        }
      }
    }
  }
  else
  {
    for (int i = 0; i < 3; i++)
      gs.flick[i] = {};
  }

  // ── Shake ──────────────────────────────────────────────────────────────
  if (cfg.enableShake)
  {
    // Shake on X and Y axes (most meaningful as physical shake gestures)
    for (int i = 0; i < 2; i++)
    {
      const int8_t s = signOf(vel[i]);
      if (s == 0 || fabsf(vel[i]) < cfg.shakeVelThresh)
        continue;
      if (s != gs.shakeSign[i] && gs.shakeSign[i] != 0)
      {
        if (gs.shakeCount[i] == 0)
          gs.shakeStartMs = nowMs;
        if (nowMs - gs.shakeStartMs < SHAKE_WINDOW_MS)
        {
          gs.shakeCount[i]++;
          if (gs.shakeCount[i] >= SHAKE_REVERSALS_REQUIRED &&
              !onCooldown)
          {
            sendGesture(GESTURE_SHAKE, encodeGestureData(i, 0));
            gs.lastGestureMs = nowMs;
            gs.shakeCount[i] = 0;
          }
        }
        else
        {
          gs.shakeCount[i] = 1;
          gs.shakeStartMs = nowMs;
        }
      }
      gs.shakeSign[i] = s;
    }
  }
  else
  {
    for (int i = 0; i < 3; i++)
    {
      gs.shakeSign[i] = 0;
      gs.shakeCount[i] = 0;
    }
    gs.shakeStartMs = 0;
  }

  // ── Double-tilt ────────────────────────────────────────────────────────
  if (cfg.enableDoubleTilt)
  {
    for (int i = 0; i < 3; i++)
    {
      for (int d = 0; d < 2; d++)
      {
        // d==0 → negative side, d==1 → positive side
        const float thresh = (d == 0) ? -cfg.doubleTiltRad
                                      : cfg.doubleTiltRad;
        const bool over = (d == 0) ? (rv[i] < thresh)
                                   : (rv[i] > thresh);
        if (over)
        {
          if (!gs.doubleTiltArmed[i][d])
            continue;
          if (gs.doubleTiltFirstMs[i][d] == 0)
          {
            gs.doubleTiltFirstMs[i][d] = nowMs;
          }
          else if ((nowMs - gs.doubleTiltFirstMs[i][d]) <
                   DOUBLE_TILT_WINDOW_MS)
          {
            if (!onCooldown)
            {
              sendGesture(GESTURE_DOUBLE_TILT,
                          encodeGestureData(i, d == 0 ? -1 : 1));
              gs.lastGestureMs = nowMs;
            }
            gs.doubleTiltFirstMs[i][d] = 0;
          }
          else
          {
            gs.doubleTiltFirstMs[i][d] = nowMs;
          }
          gs.doubleTiltArmed[i][d] = false;
        }
        else
        {
          gs.doubleTiltArmed[i][d] = true;
          if (gs.doubleTiltFirstMs[i][d] != 0 &&
              (nowMs - gs.doubleTiltFirstMs[i][d]) >=
                  DOUBLE_TILT_WINDOW_MS)
            gs.doubleTiltFirstMs[i][d] = 0;
        }
      }
    }
  }
  else
  {
    for (int i = 0; i < 3; i++)
      for (int d = 0; d < 2; d++)
      {
        gs.doubleTiltFirstMs[i][d] = 0;
        gs.doubleTiltArmed[i][d] = true;
      }
  }

  // ── Circle ─────────────────────────────────────────────────────────────
  if (cfg.enableCircle)
  {
    const uint8_t a = cfg.cursorXAxis, b = cfg.cursorYAxis;
    if (fabsf(vel[a]) > cfg.circleMinSpeed &&
        fabsf(vel[b]) > cfg.circleMinSpeed)
    {
      const int8_t sa = signOf(vel[a]), sb = signOf(vel[b]);
      if (sa != gs.circleSignA || sb != gs.circleSignB)
        gs.circleFrames++;
      gs.circleSignA = sa;
      gs.circleSignB = sb;
      if (gs.circleFrames >= CIRCLE_FRAMES_REQUIRED && !onCooldown)
      {
        sendGesture(GESTURE_CIRCLE,
                    encodeGestureData(GESTURE_AXIS_NONE, 0));
        gs.lastGestureMs = nowMs;
        gs.circleFrames = 0;
      }
    }
    else
    {
      gs.circleFrames = 0;
    }
  }
  else
  {
    gs.circleFrames = 0;
    gs.circleSignA = 0;
    gs.circleSignB = 0;
  }
}

// ─────────────────────────────────────────────────────────────────────────────
// MPU6050
// ─────────────────────────────────────────────────────────────────────────────
MPU6050 mpu;
bool dmpReady = false;
uint8_t fifoBuffer[64] = {};

// q       : raw quaternion from DMP (world frame)
// q_ref   : reference quaternion captured on recenter
Quaternion q;
Quaternion q_ref(1.f, 0.f, 0.f, 0.f); // identity
VectorInt16 gg;                       // raw gyro readings — used by bias estimator

volatile bool mpuInterrupt = false;
void IRAM_ATTR dmpDataReady() { mpuInterrupt = true; }

// ─────────────────────────────────────────────────────────────────────────────
// setup()
// ─────────────────────────────────────────────────────────────────────────────
void setup()
{
  pinMode(LED_BUILTIN, OUTPUT);
  digitalWrite(LED_BUILTIN, LOW);
  Serial.begin(115200);

  Wire.begin();
  Wire.setClock(400000);

  loadConfig();
  Serial.printf("CFG ver=%u gainX=%.1f gainY=%.1f dzX=%.3f cursorX=AXIS%u cursorY=AXIS%u click=AXIS%u\n",
                CONFIG_VERSION, cfg.gainX, cfg.gainY, cfg.deadzoneX,
                cfg.cursorXAxis, cfg.cursorYAxis, cfg.clickAxis);
  pinMode(INTERRUPT_PIN, INPUT);

  mpu.initialize();
  if (!mpu.testConnection())
  {
    while (true)
    {
      digitalWrite(LED_BUILTIN, HIGH);
      delay(200);
      digitalWrite(LED_BUILTIN, LOW);
      delay(200);
    }
  }

  if (mpu.dmpInitialize() != 0)
  {
    while (true)
    {
      digitalWrite(LED_BUILTIN, HIGH);
      delay(1000);
      digitalWrite(LED_BUILTIN, LOW);
      delay(1000);
    }
  }

  mpu.setXGyroOffset(0);
  mpu.setYGyroOffset(0);
  mpu.setZGyroOffset(0);
  mpu.setXAccelOffset(0);
  mpu.setYAccelOffset(0);
  mpu.setZAccelOffset(0);
  mpu.CalibrateAccel(6);
  mpu.CalibrateGyro(6);
  mpu.setDMPEnabled(true);
  attachInterrupt(digitalPinToInterrupt(INTERRUPT_PIN), dmpDataReady, RISING);
  dmpReady = true;

  setupHID();
  digitalWrite(LED_BUILTIN, HIGH); // solid = ready
}

// ─────────────────────────────────────────────────────────────────────────────
// loop()
// ─────────────────────────────────────────────────────────────────────────────
void loop()
{
  if (!dmpReady)
    return;

  static uint32_t lastReportMs = 0;
  static uint32_t lastSampleMs = 0;
  static bool lastTiltNeg = false;
  static bool lastTiltPos = false;

  const uint32_t nowMs = millis();
  if ((nowMs - lastSampleMs) < SAMPLE_INTERVAL_MS)
    return;
  if (!mpu.dmpGetCurrentFIFOPacket(fifoBuffer))
    return;
  lastSampleMs = nowMs;

  // ── 1. Get raw quaternion + raw gyro from DMP ──────────────────────────
  mpu.dmpGetQuaternion(&q, fifoBuffer);
  mpu.dmpGetGyro(&gg, fifoBuffer);

  // ── 2. Recenter ────────────────────────────────────────────────────────
  if (requestRecenter)
  {
    q_ref = q;
    gs = GestureState{};
    prevFrameMs = 0;
    memset(rv, 0, sizeof(rv));
    memset(prevRv, 0, sizeof(prevRv));
    memset(vel, 0, sizeof(vel));
    memset(gyroSmooth, 0, sizeof(gyroSmooth));
    stillFrames = 0;
    requestRecenter = false;
  }

  // ── 3. Relative quaternion: q_rel = q_ref⁻¹ · q ──────────────────────
  Quaternion q_rel = quatMul(quatConj(q_ref), q);

  // Normalise to guard against cumulative DMP drift
  float norm = sqrtf(q_rel.w * q_rel.w +
                     q_rel.x * q_rel.x +
                     q_rel.y * q_rel.y +
                     q_rel.z * q_rel.z);
  if (norm > 1e-6f)
  {
    q_rel.w /= norm;
    q_rel.x /= norm;
    q_rel.y /= norm;
    q_rel.z /= norm;
  }

  // ── 4. Position proxy from quaternion imaginary parts ─────────────────
  quatToPos(q_rel, rv);

  // ── 5. Angular velocity ────────────────────────────────────────────────
  // Skip on the first frame after recenter (prevFrameMs == 0).
  if (prevFrameMs != 0)
  {
    const float dt = (nowMs - prevFrameMs) * 0.001f;
    if (dt > 0.f)
    {
      for (int i = 0; i < 3; i++)
        vel[i] = (rv[i] - prevRv[i]) / dt;
    }
  }
  memcpy(prevRv, rv, sizeof(rv));
  prevFrameMs = nowMs;

  // ── 6. Drift correction + runtime bias estimation ──────────────────────
  //
  // Two complementary mechanisms running in the same still-detection window:
  //
  //  A) q_ref slerp — cleans up already-accumulated quaternion drift by
  //     pulling the reference frame toward the current reading. Fast-acting.
  //
  //  B) Gyro bias estimation — measures the raw gyro mean while still and
  //     writes it back to the MPU6050 hardware offset registers. This fixes
  //     the *source* so future integration is cleaner. Slow but permanent.
  {
    // Tune this (raw MPU6050 gyro LSB; ~131 LSB ≈ 1°/s at default scale)
    constexpr float STILL_GYRO_THRESHOLD = 25.0f;

    const bool still =
        fabsf(rv[0]) < DRIFT_STILL_THRESHOLD &&
        fabsf(rv[1]) < DRIFT_STILL_THRESHOLD &&
        fabsf(rv[2]) < DRIFT_STILL_THRESHOLD &&
        fabsf(gg.x) < STILL_GYRO_THRESHOLD &&
        fabsf(gg.y) < STILL_GYRO_THRESHOLD &&
        fabsf(gg.z) < STILL_GYRO_THRESHOLD;

    // Print on state change OR every 200 ms while still
    if (still != prevStill || (still && (nowMs - lastStillPrintMs) > 200))
    {
      Serial.printf(
          "still=%d | rv=(%.3f %.3f %.3f) | gyro=(%d %d %d) | vel=(%.2f %.2f %.2f)\n",
          still,
          rv[0], rv[1], rv[2],
          gg.x, gg.y, gg.z,
          vel[0], vel[1], vel[2]);
      lastStillPrintMs = nowMs;
    }
    prevStill = still;

    if (still)
    {
      // ── A) q_ref slerp ──────────────────────────────────────────
      if (prevFrameMs != 0)
      {
        const float t = DRIFT_RATE * (SAMPLE_INTERVAL_MS * 0.001f);
        q_ref = quatNlerp(q_ref, q, t);
        // Recompute rv immediately so cursor benefits this frame
        Quaternion q_rel2 = quatMul(quatConj(q_ref), q);
        float n2 = sqrtf(q_rel2.w * q_rel2.w + q_rel2.x * q_rel2.x +
                         q_rel2.y * q_rel2.y + q_rel2.z * q_rel2.z);
        if (n2 > 1e-6f)
        {
          q_rel2.w /= n2;
          q_rel2.x /= n2;
          q_rel2.y /= n2;
          q_rel2.z /= n2;
        }
        quatToPos(q_rel2, rv);
      }

      // ── B) Gyro bias estimation ──────────────────────────────────
      // Low-pass filter the raw gyro to build a stable mean estimate.
      // gg.x/y/z are in raw LSB (±32768 at full scale).
      gyroSmooth[0] += BIAS_LOWPASS_ALPHA * (gg.x - gyroSmooth[0]);
      gyroSmooth[1] += BIAS_LOWPASS_ALPHA * (gg.y - gyroSmooth[1]);
      gyroSmooth[2] += BIAS_LOWPASS_ALPHA * (gg.z - gyroSmooth[2]);

      stillFrames++;

      if (stillFrames >= BIAS_MIN_STILL_FRAMES)
      {
        // Convert smoothed reading to integer offset.
        // The MPU6050 offset registers expect the *negative* of the
        // measured bias (i.e. the correction to apply).
        const int16_t newBias[3] = {
            static_cast<int16_t>(-gyroSmooth[0]),
            static_cast<int16_t>(-gyroSmooth[1]),
            static_cast<int16_t>(-gyroSmooth[2])};

        // Only write if any axis has changed by more than the
        // threshold — avoids hammering I²C every frame.
        const bool changed =
            abs(newBias[0] - appliedBias[0]) > BIAS_APPLY_THRESHOLD_LSB ||
            abs(newBias[1] - appliedBias[1]) > BIAS_APPLY_THRESHOLD_LSB ||
            abs(newBias[2] - appliedBias[2]) > BIAS_APPLY_THRESHOLD_LSB;

        if (changed)
        {
          mpu.setXGyroOffset(newBias[0]);
          mpu.setYGyroOffset(newBias[1]);
          mpu.setZGyroOffset(newBias[2]);
          appliedBias[0] = newBias[0];
          appliedBias[1] = newBias[1];
          appliedBias[2] = newBias[2];
        }
      }
    }
    else
    {
      // Device is moving — reset still counter so we only commit bias
      // estimates from sustained stationary periods.
      stillFrames = 0;
    }
  }

  // ── 7. Gesture detection ───────────────────────────────────────────────
  detectGestures(nowMs);

  // ── 8. Cursor movement ─────────────────────────────────────────────────
  const float rawX = applyDeadband(rv[cfg.cursorXAxis], cfg.deadzoneX);
  const float rawY = applyDeadband(rv[cfg.cursorYAxis], cfg.deadzoneY);
  const float rawTilt = applyDeadband(rv[cfg.clickAxis], cfg.deadzoneClick);

  const float cursorX = rawX * cfg.gainX * (cfg.invertX ? -1.f : 1.f);
  const float cursorY = rawY * cfg.gainY * (cfg.invertY ? -1.f : 1.f);
  const float tiltV = rawTilt * (cfg.invertClick ? -1.f : 1.f);

  // ── 9. Tilt buttons ────────────────────────────────────────────────────
  const bool tiltNeg = hidReportsReady(nowMs) && (tiltV < -cfg.tiltThreshRad);
  const bool tiltPos = hidReportsReady(nowMs) && (tiltV > cfg.tiltThreshRad);

  uint8_t buttons = 0;
  if (cfg.enableClicks)
  {
    if (tiltNeg)
      buttons |= 0x01;
    if (tiltPos)
      buttons |= 0x02;
  }

  // ── 10. Send mouse report ──────────────────────────────────────────────
  const int moveX = constrain(static_cast<int>(roundf(cursorX)), -127, 127);
  const int moveY = constrain(static_cast<int>(roundf(-cursorY)), -127, 127);
  const bool hasMovement = (moveX != 0 || moveY != 0);
  const bool buttonChanged = (tiltNeg != lastTiltNeg || tiltPos != lastTiltPos);

  if ((hasMovement || buttonChanged) &&
      (nowMs - lastReportMs) >= BLE_REPORT_INTERVAL_MS)
  {
    sendMouse(static_cast<int8_t>(moveX),
              static_cast<int8_t>(moveY),
              buttons);
    lastReportMs = nowMs;
  }

  lastTiltNeg = tiltNeg;
  lastTiltPos = tiltPos;
}