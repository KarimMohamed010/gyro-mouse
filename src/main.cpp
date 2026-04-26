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

// Hardware
constexpr uint8_t INTERRUPT_PIN = 2;

// HID report IDs and fixed payload sizes. BLE report characteristic values do
// not include the report ID; Windows HID API buffers do include it as byte 0.
constexpr uint8_t REPORT_ID_MOUSE = 1;
constexpr uint8_t REPORT_ID_GESTURE = 2;
constexpr uint8_t REPORT_ID_FEATURE = 3;
constexpr uint8_t FEATURE_PAYLOAD_SIZE = 8;

// Fixed tuning
constexpr uint16_t BLE_POST_CONNECT_DELAY_MS = 1200;
constexpr uint16_t BLE_REPORT_INTERVAL_MS = 16;
constexpr uint16_t GESTURE_COOLDOWN_MS = 800;
constexpr uint16_t SHAKE_WINDOW_MS = 800;
constexpr uint16_t DOUBLE_TILT_WINDOW_MS = 500;
constexpr uint8_t SHAKE_REVERSALS_REQUIRED = 4;
constexpr uint8_t CIRCLE_FRAMES_REQUIRED = 12;
constexpr uint16_t SERIAL_STREAM_INTERVAL_MS = 20; // ~50 Hz

// Axis indices
constexpr uint8_t AXIS_YAW = 0;
constexpr uint8_t AXIS_PITCH = 1;
constexpr uint8_t AXIS_ROLL = 2;

// Gesture IDs sent in report ID 2.
constexpr uint8_t GESTURE_FLICK = 1;
constexpr uint8_t GESTURE_SHAKE = 2;
constexpr uint8_t GESTURE_DOUBLE_TILT = 3;
constexpr uint8_t GESTURE_CIRCLE = 4;
constexpr uint8_t GESTURE_AXIS_NONE = 3;
constexpr uint8_t GESTURE_DIRECTION_POS = 0x80;

// Feature report config pages.
constexpr uint8_t FEATURE_PAGE_BASIC = 0;
constexpr uint8_t FEATURE_PAGE_GAINS = 1;
constexpr uint8_t FEATURE_PAGE_FLICK = 2;
constexpr uint8_t FEATURE_PAGE_OTHER_GESTURES = 3;
constexpr uint8_t FEATURE_PAGE_SELECT = 0x7F;

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
};

const Config kDefaultConfig = Config{};
Config cfg = kDefaultConfig;

Preferences prefs;
float axes[3] = {}; // yaw, pitch, roll in degrees

inline int8_t signOf(float v)
{
  return (v > 0.f) ? 1 : (v < 0.f) ? -1
                                   : 0;
}

inline float applyDeadband(float v, float db)
{
  return fabsf(v) < db ? 0.f : v;
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
  cfg.tiltThreshDeg = constrain(cfg.tiltThreshDeg, 0.f, 25.5f);
  cfg.flickVelThresh = constrain(cfg.flickVelThresh, 0.f, 65535.f);
  cfg.flickReturnDeg = constrain(cfg.flickReturnDeg, 0.f, 25.5f);
  cfg.flickConfirmMs = constrain(cfg.flickConfirmMs, static_cast<uint16_t>(0), static_cast<uint16_t>(65535));
  cfg.shakeVelThresh = constrain(cfg.shakeVelThresh, 0.f, 65535.f);
  cfg.doubleTiltDeg = constrain(cfg.doubleTiltDeg, 0.f, 25.5f);
  cfg.circleMinSpeed = constrain(cfg.circleMinSpeed, 0.f, 65535.f);
}

void saveConfig()
{
  sanitizeConfig();
  prefs.begin("mpu", false);
  prefs.putBytes("cfg", &cfg, sizeof(cfg));
  prefs.end();
}

void loadConfig()
{
  prefs.begin("mpu", true);
  if (prefs.isKey("cfg"))
  {
    size_t got = prefs.getBytes("cfg", &cfg, sizeof(cfg));
    (void)got;
  }
  prefs.end();
  sanitizeConfig();
}

// HID report descriptor:
//   Report 1: mouse input, 4 bytes: buttons, x, y, wheel
//   Report 2: gesture input, 2 bytes: gesture_id, gesture_data
//   Report 3: feature config, 8 bytes
static const uint8_t hidReportDescriptor[] = {
    0x05, 0x01,       // Usage Page (Generic Desktop)
    0x09, 0x02,       // Usage (Mouse)
    0xA1, 0x01,       // Collection (Application)
    0x85, REPORT_ID_MOUSE,
    0x09, 0x01,       //   Usage (Pointer)
    0xA1, 0x00,       //   Collection (Physical)
    0x05, 0x09,       //     Usage Page (Button)
    0x19, 0x01,       //     Usage Minimum (1)
    0x29, 0x08,       //     Usage Maximum (8)
    0x15, 0x00,       //     Logical Minimum (0)
    0x25, 0x01,       //     Logical Maximum (1)
    0x95, 0x08,       //     Report Count (8)
    0x75, 0x01,       //     Report Size (1)
    0x81, 0x02,       //     Input (Data,Var,Abs)
    0x05, 0x01,       //     Usage Page (Generic Desktop)
    0x09, 0x30,       //     Usage (X)
    0x09, 0x31,       //     Usage (Y)
    0x09, 0x38,       //     Usage (Wheel)
    0x15, 0x81,       //     Logical Minimum (-127)
    0x25, 0x7F,       //     Logical Maximum (127)
    0x75, 0x08,       //     Report Size (8)
    0x95, 0x03,       //     Report Count (3)
    0x81, 0x06,       //     Input (Data,Var,Rel)
    0xC0,             //   End Collection
    0xC0,             // End Collection

    0x06, 0x00, 0xFF, // Usage Page (Vendor Defined)
    0x09, 0x01,       // Usage (Gesture)
    0xA1, 0x01,       // Collection (Application)
    0x85, REPORT_ID_GESTURE,
    0x09, 0x02,       //   Usage (Gesture Data)
    0x15, 0x00,       //   Logical Minimum (0)
    0x26, 0xFF, 0x00, //   Logical Maximum (255)
    0x75, 0x08,       //   Report Size (8)
    0x95, 0x02,       //   Report Count (2)
    0x81, 0x02,       //   Input (Data,Var,Abs)
    0xC0,             // End Collection

    0x06, 0x00, 0xFF, // Usage Page (Vendor Defined)
    0x09, 0x10,       // Usage (Config)
    0xA1, 0x01,       // Collection (Application)
    0x85, REPORT_ID_FEATURE,
    0x09, 0x11,       //   Usage (Config Data)
    0x15, 0x00,       //   Logical Minimum (0)
    0x26, 0xFF, 0x00, //   Logical Maximum (255)
    0x75, 0x08,       //   Report Size (8)
    0x95, FEATURE_PAYLOAD_SIZE,
    0xB1, 0x02,       //   Feature (Data,Var,Abs)
    0xC0              // End Collection
};

BLEHIDDevice *hid = nullptr;
BLECharacteristic *mouseInput = nullptr;
BLECharacteristic *gestureInput = nullptr;
BLECharacteristic *featureReport = nullptr;
bool hidConnected = false;
uint32_t hidConnectedSinceMs = 0;
uint8_t currentFeaturePage = FEATURE_PAGE_BASIC;

void buildFeaturePayload(uint8_t page, uint8_t out[FEATURE_PAYLOAD_SIZE])
{
  memset(out, 0, FEATURE_PAYLOAD_SIZE);
  out[0] = page;

  switch (page)
  {
  case FEATURE_PAGE_BASIC:
    out[1] = (cfg.cursorXAxis & 0x03) |
             ((cfg.cursorYAxis & 0x03) << 2) |
             ((cfg.clickAxis & 0x03) << 4);
    out[2] = (cfg.invertX ? 0x01 : 0) |
             (cfg.invertY ? 0x02 : 0) |
             (cfg.invertClick ? 0x04 : 0) |
             (cfg.enableFlick ? 0x10 : 0) |
             (cfg.enableShake ? 0x20 : 0) |
             (cfg.enableDoubleTilt ? 0x40 : 0) |
             (cfg.enableCircle ? 0x80 : 0);
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
    cfg.invertX = data[2] & 0x01;
    cfg.invertY = data[2] & 0x02;
    cfg.invertClick = data[2] & 0x04;
    cfg.enableFlick = data[2] & 0x10;
    cfg.enableShake = data[2] & 0x20;
    cfg.enableDoubleTilt = data[2] & 0x40;
    cfg.enableCircle = data[2] & 0x80;
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

class FeatureCallbacks : public BLECharacteristicCallbacks
{
  void onRead(BLECharacteristic *characteristic, esp_ble_gatts_cb_param_t *param) override
  {
    (void)characteristic;
    (void)param;
    refreshFeatureCharacteristic();
  }

  void onWrite(BLECharacteristic *characteristic, esp_ble_gatts_cb_param_t *param) override
  {
    (void)param;
    std::string value = characteristic->getValue();
    if (value.size() != FEATURE_PAYLOAD_SIZE)
      return;
    applyFeaturePayload(reinterpret_cast<const uint8_t *>(value.data()));
  }
};

class HidServerCallbacks : public BLEServerCallbacks
{
  void onConnect(BLEServer *server) override
  {
    (void)server;
    hidConnected = true;
    hidConnectedSinceMs = millis();
    Serial.println("BLE connected.");
  }

  void onDisconnect(BLEServer *server) override
  {
    hidConnected = false;
    hidConnectedSinceMs = 0;
    Serial.println("BLE disconnected. Re-advertising...");
    server->getAdvertising()->start();
  }
};

bool hidReportsReady(uint32_t nowMs)
{
  return hidConnected && ((nowMs - hidConnectedSinceMs) >= BLE_POST_CONNECT_DELAY_MS);
}

void sendMouse(int8_t x, int8_t y)
{
  if (!mouseInput || !hidReportsReady(millis()))
    return;

  uint8_t report[4] = {0, static_cast<uint8_t>(x), static_cast<uint8_t>(y), 0};
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
  uint8_t data = (axis <= AXIS_ROLL) ? axis : GESTURE_AXIS_NONE;
  if (direction > 0)
    data |= GESTURE_DIRECTION_POS;
  return data;
}

void setupHID()
{
  BLEDevice::init("ESP32 MPU Mouse");
  BLEServer *server = BLEDevice::createServer();
  server->setCallbacks(new HidServerCallbacks());

  hid = new BLEHIDDevice(server);
  mouseInput = hid->inputReport(REPORT_ID_MOUSE);
  gestureInput = hid->inputReport(REPORT_ID_GESTURE);
  featureReport = hid->featureReport(REPORT_ID_FEATURE);
  featureReport->setCallbacks(new FeatureCallbacks());

  hid->manufacturer()->setValue("Espressif");
  hid->pnp(0x02, 0xE502, 0xA111, 0x0210);
  hid->hidInfo(0x00, 0x02);
  hid->reportMap(const_cast<uint8_t *>(hidReportDescriptor), sizeof(hidReportDescriptor));
  refreshFeatureCharacteristic();
  hid->startServices();
  hid->setBatteryLevel(100);

  BLESecurity *security = new BLESecurity();
  security->setAuthenticationMode(ESP_LE_AUTH_BOND);

  BLEAdvertising *advertising = server->getAdvertising();
  advertising->setAppearance(HID_MOUSE);
  advertising->addServiceUUID(hid->hidService()->getUUID());
  advertising->start();

  Serial.println("BLE HID advertising as: ESP32 MPU Mouse");
}

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
  bool doubleTiltArmed[3][2] = {};

  uint8_t circleFrames = 0;
  int8_t circleSignA = 0;
  int8_t circleSignB = 0;
} gs;

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
    vel[i] = (axes[i] - gs.prevAxes[i]) / dt;
    gs.prevAxes[i] = axes[i];
  }
  gs.prevFrameMs = nowMs;

  const bool onCooldown = (nowMs - gs.lastGestureMs) < GESTURE_COOLDOWN_MS;

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
        {
          fa.phase = 0;
        }
        else if (fabsf(axes[i] - fa.originAngle) < cfg.flickReturnDeg)
        {
          if (!onCooldown)
          {
            sendGesture(GESTURE_FLICK, encodeGestureData(i, fa.direction));
            Serial.print("{\"gesture\":\"Flick\",\"axis\":");
            Serial.print(i);
            Serial.print(",\"dir\":");
            Serial.print(fa.direction > 0 ? 1 : -1);
            Serial.println("}");
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
            Serial.print("{\"gesture\":\"Shake\",\"axis\":");
            Serial.print(i);
            Serial.println("}");
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
            gs.doubleTiltFirstMs[i][d] = nowMs;
          }
          else if ((nowMs - gs.doubleTiltFirstMs[i][d]) < DOUBLE_TILT_WINDOW_MS)
          {
            if (!onCooldown)
            {
              sendGesture(GESTURE_DOUBLE_TILT, encodeGestureData(i, d == 0 ? -1 : 1));
              Serial.print("{\"gesture\":\"DoubleTilt\",\"axis\":");
              Serial.print(i);
              Serial.print(",\"dir\":");
              Serial.print(d == 0 ? -1 : 1);
              Serial.println("}");
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
          if (fabsf(axes[i]) < cfg.doubleTiltDeg * 0.5f &&
              gs.doubleTiltFirstMs[i][d] != 0 &&
              (nowMs - gs.doubleTiltFirstMs[i][d]) >= DOUBLE_TILT_WINDOW_MS)
          {
            gs.doubleTiltFirstMs[i][d] = 0;
          }
        }
      }
    }
  }
  else
  {
    for (int i = 0; i < 3; i++)
    {
      for (int d = 0; d < 2; d++)
      {
        gs.doubleTiltFirstMs[i][d] = 0;
        gs.doubleTiltArmed[i][d] = false;
      }
    }
  }

  if (cfg.enableCircle)
  {
    const uint8_t a = cfg.cursorXAxis;
    const uint8_t b = cfg.cursorYAxis;
    if (fabsf(vel[a]) > cfg.circleMinSpeed && fabsf(vel[b]) > cfg.circleMinSpeed)
    {
      const int8_t sa = signOf(vel[a]);
      const int8_t sb = signOf(vel[b]);
      if (sa != gs.circleSignA || sb != gs.circleSignB)
        gs.circleFrames++;
      gs.circleSignA = sa;
      gs.circleSignB = sb;
      if (gs.circleFrames >= CIRCLE_FRAMES_REQUIRED && !onCooldown)
      {
        sendGesture(GESTURE_CIRCLE, encodeGestureData(GESTURE_AXIS_NONE, 0));
        Serial.println("{\"gesture\":\"Circle\"}");
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

void handleSerial()
{
  if (!Serial.available())
    return;

  String line = Serial.readStringUntil('\n');
  line.trim();
  if (line == "{\"ping\":1}")
    Serial.println("{\"pong\":1}");
}

MPU6050 mpu;

bool dmpReady = false;
uint8_t devStatus = 0;
uint8_t fifoBuffer[64] = {0};

Quaternion q;
VectorFloat gravity;
VectorInt16 aa;
VectorInt16 gg;
float ypr[3] = {0.f, 0.f, 0.f};

volatile bool mpuInterrupt = false;
void IRAM_ATTR dmpDataReady() { mpuInterrupt = true; }

void setup()
{
  Wire.begin();
  Wire.setClock(400000);

  loadConfig();
  pinMode(INTERRUPT_PIN, INPUT);

  mpu.initialize();
  if (!mpu.testConnection())
  {
    // MPU connection failed — halt; BLE client will see no data
    while (true) delay(1000);
  }

  devStatus = mpu.dmpInitialize();
  mpu.setXGyroOffset(0);
  mpu.setYGyroOffset(0);
  mpu.setZGyroOffset(0);
  mpu.setXAccelOffset(0);
  mpu.setYAccelOffset(0);
  mpu.setZAccelOffset(0);

  if (devStatus == 0)
  {
    mpu.CalibrateAccel(6);
    mpu.CalibrateGyro(6);
    mpu.setDMPEnabled(true);
    attachInterrupt(digitalPinToInterrupt(INTERRUPT_PIN), dmpDataReady, RISING);
    dmpReady = true;
  }
  else
  {
    // DMP init failed — halt
    while (true) delay(1000);
  }

  setupHID();
  Serial.println("Serial live format: {\"a\":[yaw,pitch,roll]}");
}

void loop()
{
  if (!dmpReady)
    return;

  handleSerial();

  static uint32_t lastStatusMs = 0;
  static uint32_t lastReportMs = 0;
  static uint32_t lastSampleMs = 0;

  const uint32_t nowMs = millis();
  if (!hidConnected && nowMs - lastStatusMs > 2000)
  {
    Serial.println("Waiting for BLE...");
    lastStatusMs = nowMs;
  }

  if ((nowMs - lastSampleMs) < SERIAL_STREAM_INTERVAL_MS)
    return;

  if (!mpu.dmpGetCurrentFIFOPacket(fifoBuffer))
    return;

  lastSampleMs = nowMs;

  mpu.dmpGetQuaternion(&q, fifoBuffer);
  mpu.dmpGetAccel(&aa, fifoBuffer);
  mpu.dmpGetGyro(&gg, fifoBuffer);
  mpu.dmpGetGravity(&gravity, &q);
  mpu.dmpGetYawPitchRoll(ypr, &q, &gravity);

  axes[AXIS_YAW] = ypr[0] * RAD_TO_DEG;
  axes[AXIS_PITCH] = ypr[1] * RAD_TO_DEG;
  axes[AXIS_ROLL] = ypr[2] * RAD_TO_DEG;

  Serial.print("{\"a\":[");
  Serial.print(axes[AXIS_YAW], 3);
  Serial.print(',');
  Serial.print(axes[AXIS_PITCH], 3);
  Serial.print(',');
  Serial.print(axes[AXIS_ROLL], 3);
  Serial.println("]}");

  const float rawX = applyDeadband(getAxis(cfg.cursorXAxis), cfg.deadzoneX);
  const float rawY = applyDeadband(getAxis(cfg.cursorYAxis), cfg.deadzoneY);
  const float rawTilt = applyDeadband(getAxis(cfg.clickAxis), cfg.deadzoneClick);

  const float cursorX = rawX * cfg.gainX * (cfg.invertX ? -1.f : 1.f);
  const float cursorY = rawY * cfg.gainY * (cfg.invertY ? -1.f : 1.f);
  const float tiltV = rawTilt * (cfg.invertClick ? -1.f : 1.f);

  detectGestures(nowMs);

  const bool tiltPastNegative = hidReportsReady(nowMs) && (tiltV < -cfg.tiltThreshDeg);
  const bool tiltPastPositive = hidReportsReady(nowMs) && (tiltV > cfg.tiltThreshDeg);
  (void)tiltPastNegative;
  (void)tiltPastPositive;

  const int moveX = constrain(static_cast<int>(roundf(cursorX)), -127, 127);
  const int moveY = constrain(static_cast<int>(roundf(-cursorY)), -127, 127);

  if ((moveX != 0 || moveY != 0) && (nowMs - lastReportMs >= BLE_REPORT_INTERVAL_MS))
  {
    sendMouse(static_cast<int8_t>(moveX), static_cast<int8_t>(moveY));
    lastReportMs = nowMs;
  }
}
