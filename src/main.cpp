#include <Arduino.h>
#include "I2Cdev.h"
#include "MPU6050_6Axis_MotionApps20.h"
#include <Wire.h>
#include <BleMouse.h>
#include <BLEDevice.h>
#include <ArduinoJson.h>

// ── Hardware ──────────────────────────────────────────────────────────────────
constexpr uint8_t INTERRUPT_PIN = 2;

// ── Fixed tuning (not user-configurable) ─────────────────────────────────────
constexpr uint16_t BLE_POST_CONNECT_DELAY_MS = 1200;
constexpr uint16_t BLE_REPORT_INTERVAL_MS = 16;
constexpr uint16_t CLICK_REARM_MS = 600;
constexpr uint16_t GESTURE_COOLDOWN_MS = 800;
constexpr uint16_t SHAKE_WINDOW_MS = 800;
constexpr uint16_t DOUBLE_TILT_WINDOW_MS = 500;
constexpr uint8_t SHAKE_REVERSALS_REQUIRED = 4;
constexpr uint8_t CIRCLE_FRAMES_REQUIRED = 12;
constexpr uint16_t SERIAL_STREAM_INTERVAL_MS = 50; // 20 Hz live data to GUI

// ── Axis indices ──────────────────────────────────────────────────────────────
// The DMP gives us yaw/pitch/roll. We expose them as A0/A1/A2 to the config.
// 0 = yaw, 1 = pitch, 2 = roll
constexpr uint8_t AXIS_YAW = 0;
constexpr uint8_t AXIS_PITCH = 1;
constexpr uint8_t AXIS_ROLL = 2;

// ── User config (runtime, overwritten by GUI via Serial) ──────────────────────
struct Config
{
  // Axis mapping: which DMP axis drives each function
  uint8_t cursorXAxis = AXIS_YAW;  // default: yaw → cursor X
  uint8_t cursorYAxis = AXIS_ROLL; // default: roll → cursor Y
  uint8_t clickAxis = AXIS_PITCH;  // default: pitch → left/right click

  // Invert flags per function
  bool invertX = false;
  bool invertY = false;
  bool invertClick = false;

  // Deadzones (degrees)
  float deadzoneX = 1.5f;
  float deadzoneY = 1.5f;
  float deadzoneClick = 2.0f;

  // Sensitivity
  float gainX = 0.3f;
  float gainY = 0.3f;

  // Click threshold
  float clickThreshDeg = 30.0f;

  // Gesture thresholds
  float flickVelThresh = 120.0f;
  float flickReturnDeg = 8.0f;
  uint16_t flickConfirmMs = 300;
  float shakeVelThresh = 60.0f;
  float doubleTiltDeg = 25.0f;
  float circleMinSpeed = 20.0f;

  // Gesture enable switches
  bool enableFlick = true;
  bool enableShake = true;
  bool enableDoubleTilt = true;
  bool enableCircle = true;
} cfg;

// ── Helpers ───────────────────────────────────────────────────────────────────
inline int8_t signOf(float v) { return (v > 0.f) ? 1 : (v < 0.f) ? -1
                                                                 : 0; }
inline float applyDeadband(float v, float db) { return fabsf(v) < db ? 0.f : v; }

// Raw DMP angles array indexed by AXIS_* constants
float axes[3] = {}; // [yaw, pitch, roll] in degrees, updated each frame

inline float getAxis(uint8_t idx) { return axes[idx]; }

// ── Gesture state ─────────────────────────────────────────────────────────────
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
  } flick[3]; // one per axis

  int8_t shakeSign[3] = {};
  uint8_t shakeCount[3] = {};
  uint32_t shakeStartMs = 0;

  uint32_t doubleTiltFirstMs[3][2] = {};
  bool doubleTiltArmed[3][2] = {};

  uint8_t circleFrames = 0;
  int8_t circleSignA = 0;
  int8_t circleSignB = 0;
} gs;

static const char *AXIS_NAMES[3] = {"YAW", "PITCH", "ROLL"};
static const char *AXIS_POS[3] = {"YAW+", "PITCH+", "ROLL+"};
static const char *AXIS_NEG[3] = {"YAW-", "PITCH-", "ROLL-"};

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

  // ── Flick (arm + return-to-origin confirm) ────────────────────────────────
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
            Serial.print("{\"gesture\":\"Flick\",\"axis\":\"");
            Serial.print(fa.direction > 0 ? AXIS_POS[i] : AXIS_NEG[i]);
            Serial.println("\"}");
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
    {
      gs.flick[i].phase = 0;
      gs.flick[i].direction = 0;
      gs.flick[i].originAngle = 0.f;
      gs.flick[i].armedMs = 0;
    }
  }

  // ── Shake ─────────────────────────────────────────────────────────────────
  if (cfg.enableShake)
  {
    for (int i = 0; i < 2; i++)
    { // yaw + roll only (pitch = click axis)
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
            Serial.print("{\"gesture\":\"Shake\",\"axis\":\"");
            Serial.print(AXIS_NAMES[i]);
            Serial.println("\"}");
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

  // ── Double-tilt ───────────────────────────────────────────────────────────
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
              Serial.print("{\"gesture\":\"DoubleTilt\",\"axis\":\"");
              Serial.print(d == 0 ? AXIS_NEG[i] : AXIS_POS[i]);
              Serial.println("\"}");
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

  // ── Circle (cursor X + Y axes moving 90° out of phase) ───────────────────
  if (cfg.enableCircle)
  {
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
  }
  else
  {
    gs.circleFrames = 0;
    gs.circleSignA = 0;
    gs.circleSignB = 0;
  }
}

// ── Serial config parser ──────────────────────────────────────────────────────
// Expected format: {"cfg":{"cursorXAxis":0,"deadzoneX":1.5,...}}
void handleSerial()
{
  if (!Serial.available())
    return;
  String line = Serial.readStringUntil('\n');
  line.trim();
  if (line.length() == 0)
    return;

  // Handle ping
  if (line == "{\"ping\":1}")
  {
    Serial.println("{\"pong\":1}");
    return;
  }

  JsonDocument doc;
  if (deserializeJson(doc, line) != DeserializationError::Ok)
    return;
  if (!doc["cfg"].is<JsonObject>())
    return;

  JsonObject c = doc["cfg"].as<JsonObject>();
  if (c["cursorXAxis"].is<uint8_t>())
    cfg.cursorXAxis = c["cursorXAxis"].as<uint8_t>();
  if (c["cursorYAxis"].is<uint8_t>())
    cfg.cursorYAxis = c["cursorYAxis"].as<uint8_t>();
  if (c["clickAxis"].is<uint8_t>())
    cfg.clickAxis = c["clickAxis"].as<uint8_t>();
  if (c["invertX"].is<bool>())
    cfg.invertX = c["invertX"].as<bool>();
  if (c["invertY"].is<bool>())
    cfg.invertY = c["invertY"].as<bool>();
  if (c["invertClick"].is<bool>())
    cfg.invertClick = c["invertClick"].as<bool>();
  if (c["deadzoneX"].is<float>())
    cfg.deadzoneX = c["deadzoneX"].as<float>();
  if (c["deadzoneY"].is<float>())
    cfg.deadzoneY = c["deadzoneY"].as<float>();
  if (c["deadzoneClick"].is<float>())
    cfg.deadzoneClick = c["deadzoneClick"].as<float>();
  if (c["gainX"].is<float>())
    cfg.gainX = c["gainX"].as<float>();
  if (c["gainY"].is<float>())
    cfg.gainY = c["gainY"].as<float>();
  if (c["clickThreshDeg"].is<float>())
    cfg.clickThreshDeg = c["clickThreshDeg"].as<float>();
  if (c["flickVelThresh"].is<float>())
    cfg.flickVelThresh = c["flickVelThresh"].as<float>();
  if (c["flickReturnDeg"].is<float>())
    cfg.flickReturnDeg = c["flickReturnDeg"].as<float>();
  if (c["flickConfirmMs"].is<uint16_t>())
    cfg.flickConfirmMs = c["flickConfirmMs"].as<uint16_t>();
  if (c["shakeVelThresh"].is<float>())
    cfg.shakeVelThresh = c["shakeVelThresh"].as<float>();
  if (c["doubleTiltDeg"].is<float>())
    cfg.doubleTiltDeg = c["doubleTiltDeg"].as<float>();
  if (c["circleMinSpeed"].is<float>())
    cfg.circleMinSpeed = c["circleMinSpeed"].as<float>();
  if (c["enableFlick"].is<bool>())
    cfg.enableFlick = c["enableFlick"].as<bool>();
  if (c["enableShake"].is<bool>())
    cfg.enableShake = c["enableShake"].as<bool>();
  if (c["enableDoubleTilt"].is<bool>())
    cfg.enableDoubleTilt = c["enableDoubleTilt"].as<bool>();
  if (c["enableCircle"].is<bool>())
    cfg.enableCircle = c["enableCircle"].as<bool>();

  Serial.println("{\"ack\":\"cfg\"}");
}

// ── DMP / BLE objects ─────────────────────────────────────────────────────────
MPU6050 mpu;
BleMouse bleMouse("ESP32 MPU Mouse", "Espressif", 100);

bool dmpReady = false;
uint8_t devStatus;
uint16_t packetSize;
uint8_t fifoBuffer[64];
Quaternion q;
VectorFloat gravity;
float ypr[3];

volatile bool mpuInterrupt = false;
void IRAM_ATTR dmpDataReady() { mpuInterrupt = true; }

// ── Setup ─────────────────────────────────────────────────────────────────────
void setup()
{
  Wire.begin();
  Wire.setClock(400000);
  Serial.begin(115200);
  while (!Serial)
    ;
  pinMode(INTERRUPT_PIN, INPUT);

  Serial.println("Initializing MPU6050...");
  mpu.initialize();
  if (!mpu.testConnection())
  {
    Serial.println("MPU6050 connection failed — halting.");
    while (true)
      ;
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
    Serial.println("Auto-calibrating — keep sensor still...");
    mpu.CalibrateAccel(6);
    mpu.CalibrateGyro(6);
    mpu.PrintActiveOffsets();
    mpu.setDMPEnabled(true);
    attachInterrupt(digitalPinToInterrupt(INTERRUPT_PIN), dmpDataReady, RISING);
    dmpReady = true;
    packetSize = mpu.dmpGetFIFOPacketSize();
    Serial.println("DMP ready.");
  }
  else
  {
    Serial.print("DMP init failed (code ");
    Serial.print(devStatus);
    Serial.println(").");
    while (true)
      ;
  }

  bleMouse.begin();
  Serial.println("BLE advertising as: ESP32 MPU Mouse");
}

// ── Loop ──────────────────────────────────────────────────────────────────────
void loop()
{
  if (!dmpReady)
    return;
  handleSerial();

  static uint32_t lastStatusMs = 0;
  static uint32_t lastAdvertiseKickMs = 0;
  static uint32_t connectedSinceMs = 0;
  static uint32_t lastReportMs = 0;
  static uint32_t lastStreamMs = 0;
  static uint32_t lastLeftClickMs = 0;
  static uint32_t lastRightClickMs = 0;
  static bool clickLeftArmed = true;
  static bool clickRightArmed = true;
  static bool wasConnected = false;

  const uint32_t nowMs = millis();
  const bool connected = bleMouse.isConnected();

  if (connected && !wasConnected)
  {
    connectedSinceMs = nowMs;
    lastReportMs = nowMs;
    Serial.println("BLE connected.");
  }
  if (!connected && wasConnected)
  {
    BLEDevice::startAdvertising();
    lastAdvertiseKickMs = nowMs;
    Serial.println("BLE disconnected. Re-advertising...");
  }
  wasConnected = connected;

  if (!connected)
  {
    if (nowMs - lastAdvertiseKickMs > 5000)
    {
      BLEDevice::startAdvertising();
      lastAdvertiseKickMs = nowMs;
    }
    if (nowMs - lastStatusMs > 2000)
    {
      Serial.println("Waiting for BLE...");
      lastStatusMs = nowMs;
    }
    return;
  }
  if ((nowMs - connectedSinceMs) < BLE_POST_CONNECT_DELAY_MS)
    return;

  // ── Read DMP ──────────────────────────────────────────────────────────────
  if (!mpu.dmpGetCurrentFIFOPacket(fifoBuffer))
    return;
  mpu.dmpGetQuaternion(&q, fifoBuffer);
  mpu.dmpGetGravity(&gravity, &q);
  mpu.dmpGetYawPitchRoll(ypr, &q, &gravity);

  // Populate generic axes array: [0]=yaw, [1]=pitch, [2]=roll
  axes[AXIS_YAW] = ypr[0] * RAD_TO_DEG;
  axes[AXIS_PITCH] = ypr[1] * RAD_TO_DEG;
  axes[AXIS_ROLL] = ypr[2] * RAD_TO_DEG;

  // ── Live stream to GUI ────────────────────────────────────────────────────
  if (nowMs - lastStreamMs >= SERIAL_STREAM_INTERVAL_MS)
  {
    Serial.print("{\"a\":[");
    Serial.print(axes[0], 2);
    Serial.print(",");
    Serial.print(axes[1], 2);
    Serial.print(",");
    Serial.print(axes[2], 2);
    Serial.println("]}");
    lastStreamMs = nowMs;
  }

  // ── Apply config: map axes → cursor X/Y and click ─────────────────────────
  const float rawX = applyDeadband(getAxis(cfg.cursorXAxis), cfg.deadzoneX);
  const float rawY = applyDeadband(getAxis(cfg.cursorYAxis), cfg.deadzoneY);
  const float rawClick = applyDeadband(getAxis(cfg.clickAxis), cfg.deadzoneClick);

  const float cursorX = rawX * cfg.gainX * (cfg.invertX ? -1.f : 1.f);
  const float cursorY = rawY * cfg.gainY * (cfg.invertY ? -1.f : 1.f);
  const float clickV = rawClick * (cfg.invertClick ? -1.f : 1.f);

  // ── Gesture detection ─────────────────────────────────────────────────────
  detectGestures(nowMs);

  // ── Click axis → left / right click ──────────────────────────────────────
  if (clickV < -cfg.clickThreshDeg)
  {
    if (clickLeftArmed && (nowMs - lastLeftClickMs >= CLICK_REARM_MS))
    {
      bleMouse.click(MOUSE_LEFT);
      lastLeftClickMs = nowMs;
      clickLeftArmed = false;
    }
  }
  else
  {
    clickLeftArmed = true;
  }

  if (clickV > cfg.clickThreshDeg)
  {
    if (clickRightArmed && (nowMs - lastRightClickMs >= CLICK_REARM_MS))
    {
      bleMouse.click(MOUSE_RIGHT);
      lastRightClickMs = nowMs;
      clickRightArmed = false;
    }
  }
  else
  {
    clickRightArmed = true;
  }

  // ── Cursor movement ───────────────────────────────────────────────────────
  const int moveX = constrain(static_cast<int>(roundf(cursorX)), -127, 127);
  const int moveY = constrain(static_cast<int>(roundf(-cursorY)), -127, 127);

  if ((moveX != 0 || moveY != 0) && (nowMs - lastReportMs >= BLE_REPORT_INTERVAL_MS))
  {
    bleMouse.move(static_cast<int8_t>(moveX), static_cast<int8_t>(moveY));
    lastReportMs = nowMs;
  }
}