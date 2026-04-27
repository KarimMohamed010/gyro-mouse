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

// ─── Axis indices ─────────────────────────────────────────────────────────────
constexpr uint8_t AXIS_YAW = 0;
constexpr uint8_t AXIS_PITCH = 1;
constexpr uint8_t AXIS_ROLL = 2;

// Mount correction for the board rotation: Z 180 deg, then Y 90 deg.
constexpr float MOUNT_QW = 0.0f;
constexpr float MOUNT_QX = -0.70710678f;
constexpr float MOUNT_QY = 0.0f;
constexpr float MOUNT_QZ = -0.70710678f;

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
constexpr uint8_t CONFIG_VERSION = 2;

// ─────────────────────────────────────────────────────────────────────────────
// Config
// ─────────────────────────────────────────────────────────────────────────────
struct Config
{
  uint8_t cursorXAxis = AXIS_YAW;
  uint8_t cursorYAxis = AXIS_ROLL;
  uint8_t clickAxis = AXIS_PITCH;

  bool invertX = false;
  bool invertY = false;
  bool invertClick = false;

  float deadzoneX = 1.5f;
  float deadzoneY = 1.5f;
  float deadzoneClick = 2.0f;

  float gainX = 0.3f;
  float gainY = 0.3f;

  float tiltThreshDeg = 30.0f;

  float flickVelThresh = 120.0f;
  float flickReturnDeg = 8.0f;
  uint16_t flickConfirmMs = 300;
  float shakeVelThresh = 60.0f;
  float doubleTiltDeg = 25.0f;
  float circleMinSpeed = 20.0f;

  bool enableFlick = true;
  bool enableShake = true;
  bool enableDoubleTilt = true;
  bool enableCircle = true;
  bool enableClicks = true;
};

Config cfg;
Preferences prefs;
float axes[3] = {};
float idleOffset[3] = {};
volatile bool requestRecenter = false;

// ─────────────────────────────────────────────────────────────────────────────
// Utility
// ─────────────────────────────────────────────────────────────────────────────
inline int8_t signOf(float v)
{
  return (v > 0.f) ? 1 : (v < 0.f) ? -1
                                   : 0;
}
inline float applyDeadband(float v, float db)
{
  return fabsf(v) < db ? 0.f : v;
}
inline float angleDeltaDeg(float currentDeg, float referenceDeg)
{
  float delta = currentDeg - referenceDeg;
  while (delta > 180.f)
    delta -= 360.f;
  while (delta < -180.f)
    delta += 360.f;
  return delta;
}
inline float getAxis(uint8_t idx)
{
  return (idx < 3) ? axes[idx] : 0.f;
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
  return (axis <= AXIS_ROLL) ? axis : fallback;
}

void sanitizeConfig()
{
  cfg.cursorXAxis = sanitizeAxis(cfg.cursorXAxis, AXIS_YAW);
  cfg.cursorYAxis = sanitizeAxis(cfg.cursorYAxis, AXIS_ROLL);
  cfg.clickAxis = sanitizeAxis(cfg.clickAxis, AXIS_PITCH);
  cfg.deadzoneX = constrain(cfg.deadzoneX, 0.f, 25.5f);
  cfg.deadzoneY = constrain(cfg.deadzoneY, 0.f, 25.5f);
  cfg.deadzoneClick = constrain(cfg.deadzoneClick, 0.f, 25.5f);
  cfg.gainX = constrain(cfg.gainX, 0.f, 655.35f);
  cfg.gainY = constrain(cfg.gainY, 0.f, 655.35f);
  cfg.tiltThreshDeg = constrain(cfg.tiltThreshDeg, 0.f, 90.0f);
  cfg.flickVelThresh = constrain(cfg.flickVelThresh, 0.f, 65535.f);
  cfg.flickReturnDeg = constrain(cfg.flickReturnDeg, 0.f, 25.5f);
  cfg.flickConfirmMs = (uint16_t)constrain((int)cfg.flickConfirmMs, 0, 65535);
  cfg.shakeVelThresh = constrain(cfg.shakeVelThresh, 0.f, 65535.f);
  cfg.doubleTiltDeg = constrain(cfg.doubleTiltDeg, 0.f, 25.5f);
  cfg.circleMinSpeed = constrain(cfg.circleMinSpeed, 0.f, 65535.f);
}

void saveConfig()
{
  sanitizeConfig();
  prefs.begin("mpu", false);
  prefs.putUChar("cfgver", CONFIG_VERSION);
  prefs.putBytes("cfg", &cfg, sizeof(cfg));
  prefs.end();
}

void loadConfig()
{
  cfg = Config{};
  prefs.begin("mpu", true);
  const bool hasCurrentConfig = prefs.isKey("cfg") &&
                                prefs.getUChar("cfgver", 0) == CONFIG_VERSION;
  if (hasCurrentConfig)
    prefs.getBytes("cfg", &cfg, sizeof(cfg));
  prefs.end();
  sanitizeConfig();
  if (!hasCurrentConfig)
    saveConfig();
}

// ─────────────────────────────────────────────────────────────────────────────
// HID report descriptor
//   Report 1 (input)   mouse   : buttons(1) x(1) y(1) wheel(1)
//   Report 2 (input)   gesture : id(1) data(1)
//   Report 3 (feature) config  : 8 bytes paged
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
  BLE2902 *cccd = static_cast<BLE2902 *>(input->getDescriptorByUUID(BLEUUID((uint16_t)0x2902)));
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
// ─────────────────────────────────────────────────────────────────────────────
void buildFeaturePayload(uint8_t page, uint8_t out[FEATURE_PAYLOAD_SIZE])
{
  memset(out, 0, FEATURE_PAYLOAD_SIZE);
  out[0] = page;
  switch (page)
  {
  case FEATURE_PAGE_BASIC:
    out[1] = ((cfg.cursorXAxis & 0x03) | ((cfg.cursorYAxis & 0x03) << 2) | ((cfg.clickAxis & 0x03) << 4));
    out[2] = (cfg.invertX ? 0x01 : 0) | (cfg.invertY ? 0x02 : 0) | (cfg.invertClick ? 0x04 : 0) | (cfg.enableClicks ? 0x08 : 0) | (cfg.enableFlick ? 0x10 : 0) | (cfg.enableShake ? 0x20 : 0) | (cfg.enableDoubleTilt ? 0x40 : 0) | (cfg.enableCircle ? 0x80 : 0);
    out[3] = clampU8(cfg.deadzoneX, 10.f);
    out[4] = clampU8(cfg.deadzoneY, 10.f);
    out[5] = clampU8(cfg.deadzoneClick, 10.f);
    out[6] = clampU8(cfg.tiltThreshDeg, 10.f);
    break;
  case FEATURE_PAGE_GAINS:
    writeU16LE(&out[1], clampU16(cfg.gainX, 100.f));
    writeU16LE(&out[3], clampU16(cfg.gainY, 100.f));
    break;
  case FEATURE_PAGE_FLICK:
    writeU16LE(&out[1], clampU16(cfg.flickVelThresh, 1.f));
    out[3] = clampU8(cfg.flickReturnDeg, 10.f);
    writeU16LE(&out[4], cfg.flickConfirmMs);
    break;
  case FEATURE_PAGE_OTHER_GESTURES:
    writeU16LE(&out[1], clampU16(cfg.shakeVelThresh, 1.f));
    out[3] = clampU8(cfg.doubleTiltDeg, 10.f);
    writeU16LE(&out[4], clampU16(cfg.circleMinSpeed, 1.f));
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
    if (data[1] == 1) requestRecenter = true;
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
    cfg.cursorXAxis = sanitizeAxis(data[1] & 0x03, AXIS_YAW);
    cfg.cursorYAxis = sanitizeAxis((data[1] >> 2) & 0x03, AXIS_ROLL);
    cfg.clickAxis = sanitizeAxis((data[1] >> 4) & 0x03, AXIS_PITCH);
    cfg.invertX = (data[2] & 0x01) != 0;
    cfg.invertY = (data[2] & 0x02) != 0;
    cfg.invertClick = (data[2] & 0x04) != 0;
    cfg.enableClicks = (data[2] & 0x08) != 0;
    cfg.enableFlick = (data[2] & 0x10) != 0;
    cfg.enableShake = (data[2] & 0x20) != 0;
    cfg.enableDoubleTilt = (data[2] & 0x40) != 0;
    cfg.enableCircle = (data[2] & 0x80) != 0;
    cfg.deadzoneX = data[3] / 10.f;
    cfg.deadzoneY = data[4] / 10.f;
    cfg.deadzoneClick = data[5] / 10.f;
    cfg.tiltThreshDeg = data[6] / 10.f;
    break;
  case FEATURE_PAGE_GAINS:
    cfg.gainX = readU16LE(&data[1]) / 100.f;
    cfg.gainY = readU16LE(&data[3]) / 100.f;
    break;
  case FEATURE_PAGE_FLICK:
    cfg.flickVelThresh = static_cast<float>(readU16LE(&data[1]));
    cfg.flickReturnDeg = data[3] / 10.f;
    cfg.flickConfirmMs = readU16LE(&data[4]);
    break;
  case FEATURE_PAGE_OTHER_GESTURES:
    cfg.shakeVelThresh = static_cast<float>(readU16LE(&data[1]));
    cfg.doubleTiltDeg = data[3] / 10.f;
    cfg.circleMinSpeed = static_cast<float>(readU16LE(&data[4]));
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
    server->getAdvertising()->start(); // re-advertise automatically
  }
};

// ─────────────────────────────────────────────────────────────────────────────
// Report sending
// ─────────────────────────────────────────────────────────────────────────────
bool hidReportsReady(uint32_t nowMs)
{
  return hidConnected && ((nowMs - hidConnectedSinceMs) >= BLE_POST_CONNECT_DELAY_MS);
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
  uint8_t d = (axis <= AXIS_ROLL) ? axis : GESTURE_AXIS_NONE;
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
// Gesture state
// ─────────────────────────────────────────────────────────────────────────────
struct GestureState
{
  uint32_t lastGestureMs = 0;
  float prevAxes[3] = {};
  uint32_t prevFrameMs = 0;

  struct FlickAxis
  {
    uint8_t phase = 0;
    int8_t direction = 0;
    float originAngle = 0.f;
    uint32_t armedMs = 0;
  } flick[3];

  int8_t shakeSign[3] = {};
  uint8_t shakeCount[3] = {};
  uint32_t shakeStartMs = 0;

  uint32_t doubleTiltFirstMs[3][2] = {};
  // true = first tilt crossing will be registered (correct initial state)
  bool doubleTiltArmed[3][2] = {
      {true, true}, {true, true}, {true, true}};

  uint8_t circleFrames = 0;
  int8_t circleSignA = 0;
  int8_t circleSignB = 0;
} gs;

// ─────────────────────────────────────────────────────────────────────────────
// Gesture detection
// ─────────────────────────────────────────────────────────────────────────────
void detectGestures(uint32_t nowMs)
{
  if (gs.prevFrameMs == 0)
  {
    for (int i = 0; i < 3; i++)
      gs.prevAxes[i] = axes[i];
    gs.prevFrameMs = nowMs;
    return;
  }
  const float dt = (nowMs - gs.prevFrameMs) * 0.001f;
  if (dt <= 0.f)
    return;

  float vel[3];
  for (int i = 0; i < 3; i++)
  {
    vel[i] = angleDeltaDeg(axes[i], gs.prevAxes[i]) / dt;
    gs.prevAxes[i] = axes[i];
  }
  gs.prevFrameMs = nowMs;

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
          fa.originAngle = axes[i];
          fa.armedMs = nowMs;
        }
      }
      else
      {
        if (nowMs - fa.armedMs > cfg.flickConfirmMs)
          fa.phase = 0;
        else if (fabsf(angleDeltaDeg(axes[i], fa.originAngle)) < cfg.flickReturnDeg)
        {
          if (!onCooldown)
          {
            sendGesture(GESTURE_FLICK, encodeGestureData(i, fa.direction));
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
          if (gs.shakeCount[i] >= SHAKE_REVERSALS_REQUIRED && !onCooldown)
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
        const float thresh = (d == 0) ? -cfg.doubleTiltDeg : cfg.doubleTiltDeg;
        const bool over = (d == 0) ? (axes[i] < thresh) : (axes[i] > thresh);
        if (over)
        {
          if (!gs.doubleTiltArmed[i][d])
            continue;
          if (gs.doubleTiltFirstMs[i][d] == 0)
          {
            gs.doubleTiltFirstMs[i][d] = nowMs; // tilt #1
          }
          else if ((nowMs - gs.doubleTiltFirstMs[i][d]) < DOUBLE_TILT_WINDOW_MS)
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
            gs.doubleTiltFirstMs[i][d] = nowMs; // window expired, reset
          }
          gs.doubleTiltArmed[i][d] = false;
        }
        else
        {
          gs.doubleTiltArmed[i][d] = true;
          if (gs.doubleTiltFirstMs[i][d] != 0 && (nowMs - gs.doubleTiltFirstMs[i][d]) >= DOUBLE_TILT_WINDOW_MS)
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
    if (fabsf(vel[a]) > cfg.circleMinSpeed && fabsf(vel[b]) > cfg.circleMinSpeed)
    {
      const int8_t sa = signOf(vel[a]), sb = signOf(vel[b]);
      if (sa != gs.circleSignA || sb != gs.circleSignB)
        gs.circleFrames++;
      gs.circleSignA = sa;
      gs.circleSignB = sb;
      if (gs.circleFrames >= CIRCLE_FRAMES_REQUIRED && !onCooldown)
      {
        sendGesture(GESTURE_CIRCLE, encodeGestureData(GESTURE_AXIS_NONE, 0));
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
Quaternion q;
Quaternion qMountCorrection(MOUNT_QW, MOUNT_QX, MOUNT_QY, MOUNT_QZ);
VectorFloat gravity;
VectorInt16 aa, gg;
float ypr[3] = {};

volatile bool mpuInterrupt = false;
void IRAM_ATTR dmpDataReady() { mpuInterrupt = true; }

// ─────────────────────────────────────────────────────────────────────────────
// setup() — no Serial dependency, fully standalone
// ─────────────────────────────────────────────────────────────────────────────
void setup()
{
  // LED used only for fatal hardware error signalling — no Serial needed
  pinMode(LED_BUILTIN, OUTPUT);
  digitalWrite(LED_BUILTIN, LOW);

  Wire.begin();
  Wire.setClock(400000);

  loadConfig();
  pinMode(INTERRUPT_PIN, INPUT);

  mpu.initialize();
  if (!mpu.testConnection())
  {
    // Fast blink = MPU not found
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
    // Slow blink = DMP init failed
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

  // Solid LED = ready and advertising
  digitalWrite(LED_BUILTIN, HIGH);
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

  mpu.dmpGetQuaternion(&q, fifoBuffer);
  mpu.dmpGetAccel(&aa, fifoBuffer);
  mpu.dmpGetGyro(&gg, fifoBuffer);
  Quaternion qCorrected = q.getProduct(qMountCorrection);
  qCorrected.normalize();
  mpu.dmpGetGravity(&gravity, &qCorrected);
  mpu.dmpGetYawPitchRoll(ypr, &qCorrected, &gravity);

  const float yawDeg = ypr[0] * RAD_TO_DEG;
  const float pitchDeg = ypr[1] * RAD_TO_DEG;
  const float rollDeg = ypr[2] * RAD_TO_DEG;

  if (requestRecenter) {
      idleOffset[AXIS_YAW] = yawDeg;
      idleOffset[AXIS_PITCH] = pitchDeg;
      idleOffset[AXIS_ROLL] = rollDeg;
      gs = GestureState{};
      requestRecenter = false;
  }

  axes[AXIS_YAW] = angleDeltaDeg(yawDeg, idleOffset[AXIS_YAW]);
  axes[AXIS_PITCH] = angleDeltaDeg(pitchDeg, idleOffset[AXIS_PITCH]);
  axes[AXIS_ROLL] = angleDeltaDeg(rollDeg, idleOffset[AXIS_ROLL]);

  const float rawX = applyDeadband(getAxis(cfg.cursorXAxis), cfg.deadzoneX);
  const float rawY = applyDeadband(getAxis(cfg.cursorYAxis), cfg.deadzoneY);
  const float rawTilt = applyDeadband(getAxis(cfg.clickAxis), cfg.deadzoneClick);

  const float cursorX = rawX * cfg.gainX * (cfg.invertX ? -1.f : 1.f);
  const float cursorY = rawY * cfg.gainY * (cfg.invertY ? -1.f : 1.f);
  const float tiltV = rawTilt * (cfg.invertClick ? -1.f : 1.f);

  detectGestures(nowMs);

  const bool tiltNeg = hidReportsReady(nowMs) && (tiltV < -cfg.tiltThreshDeg);
  const bool tiltPos = hidReportsReady(nowMs) && (tiltV > cfg.tiltThreshDeg);

  uint8_t buttons = 0;
  if (cfg.enableClicks) {
    if (tiltNeg)
      buttons |= 0x01;
    if (tiltPos)
      buttons |= 0x02;
  }

  const int moveX = constrain(static_cast<int>(roundf(cursorX)), -127, 127);
  const int moveY = constrain(static_cast<int>(roundf(-cursorY)), -127, 127);
  const bool hasMovement = (moveX != 0 || moveY != 0);
  const bool buttonChanged = (tiltNeg != lastTiltNeg || tiltPos != lastTiltPos);

  if ((hasMovement || buttonChanged) && (nowMs - lastReportMs) >= BLE_REPORT_INTERVAL_MS)
  {
    sendMouse(static_cast<int8_t>(moveX),
              static_cast<int8_t>(moveY),
              buttons);
    lastReportMs = nowMs;
  }

  lastTiltNeg = tiltNeg;
  lastTiltPos = tiltPos;
}
