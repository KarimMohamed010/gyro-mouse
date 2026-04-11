#include <Arduino.h>
#include "I2Cdev.h"
#include "MPU6050_6Axis_MotionApps20.h"
#include <Wire.h>
#include <BleMouse.h>
#include <BLEDevice.h>

// ── Hardware ──────────────────────────────────────────────────────────────────
constexpr uint8_t INTERRUPT_PIN = 2; // MPU6050 INT → GPIO2

// ── Tuning ────────────────────────────────────────────────────────────────────
constexpr float ANGLE_DEADBAND_DEG = 1.5f;       // ignore angles within this range of zero
constexpr float ANGLE_TO_MOUSE_GAIN = 0.3f;      // angle (deg) → mouse counts per frame
constexpr float YAW_CLICK_THRESHOLD_DEG = 30.0f; // yaw past this → left/right click
constexpr uint16_t CLICK_REARM_MS = 600;         // min time before same click can fire again
constexpr uint16_t BLE_POST_CONNECT_DELAY_MS = 1200;
constexpr uint16_t BLE_REPORT_INTERVAL_MS = 16;

// ── Objects ───────────────────────────────────────────────────────────────────
MPU6050 mpu;
BleMouse bleMouse("ESP32 MPU Mouse", "Espressif", 100);

// ── DMP state ─────────────────────────────────────────────────────────────────
bool dmpReady = false;
uint8_t devStatus;
uint16_t packetSize;
uint8_t fifoBuffer[64];

Quaternion q;
VectorFloat gravity;
float ypr[3]; // [yaw, pitch, roll] in radians

volatile bool mpuInterrupt = false;
void IRAM_ATTR dmpDataReady() { mpuInterrupt = true; }

// ── Helpers ───────────────────────────────────────────────────────────────────
inline float applyDeadband(float v, float db)
{
  return (fabsf(v) < db) ? 0.0f : v;
}

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
  Serial.println("MPU6050 connection OK.");

  Serial.println("Initializing DMP...");
  devStatus = mpu.dmpInitialize();

  // Zero out offsets — CalibrateAccel/Gyro will find the real values
  mpu.setXGyroOffset(0);
  mpu.setYGyroOffset(0);
  mpu.setZGyroOffset(0);
  mpu.setXAccelOffset(0);
  mpu.setYAccelOffset(0);
  mpu.setZAccelOffset(0);

  if (devStatus == 0)
  {
    // Auto-calibration: keep MPU6050 flat and still during this ~2 s window
    Serial.println("Auto-calibrating — keep sensor still...");
    mpu.CalibrateAccel(6);
    mpu.CalibrateGyro(6);
    Serial.println("Calibration done. Active offsets:");
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
    Serial.println("). Halting.");
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

  static uint32_t lastStatusMs = 0;
  static uint32_t lastAdvertiseKickMs = 0;
  static uint32_t connectedSinceMs = 0;
  static uint32_t lastReportMs = 0;
  static uint32_t lastLeftClickMs = 0;
  static uint32_t lastRightClickMs = 0;
  static bool yawLeftArmed = true;  // ready to fire left click
  static bool yawRightArmed = true; // ready to fire right click
  static bool wasConnected = false;

  const uint32_t nowMs = millis();
  const bool connected = bleMouse.isConnected();

  // ── Connection bookkeeping ─────────────────────────────────────────────────
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
    if (nowMs - lastStatusMs > 1000)
    {
      Serial.println("Waiting for BLE connection...");
      lastStatusMs = nowMs;
    }
    return;
  }

  if ((nowMs - connectedSinceMs) < BLE_POST_CONNECT_DELAY_MS)
  {
    if (nowMs - lastStatusMs > 1000)
    {
      Serial.println("Link up — waiting for HID to be ready...");
      lastStatusMs = nowMs;
    }
    return;
  }

  // ── DMP packet → orientation ──────────────────────────────────────────────
  if (!mpu.dmpGetCurrentFIFOPacket(fifoBuffer))
    return;

  mpu.dmpGetQuaternion(&q, fifoBuffer);
  mpu.dmpGetGravity(&gravity, &q);
  mpu.dmpGetYawPitchRoll(ypr, &q, &gravity);

  // ypr[0]=yaw, ypr[1]=pitch, ypr[2]=roll (radians)
  const float yaw = ypr[0] * RAD_TO_DEG;
  const float pitch = applyDeadband(ypr[1] * RAD_TO_DEG, ANGLE_DEADBAND_DEG);
  const float roll = applyDeadband(ypr[2] * RAD_TO_DEG, ANGLE_DEADBAND_DEG);

  // ── Yaw tilt → clicks (one-shot, re-arms when returned past threshold) ────
  // Tilt left  (yaw < -threshold) → left click
  // Tilt right (yaw >  threshold) → right click
  if (yaw < -YAW_CLICK_THRESHOLD_DEG)
  {
    if (yawLeftArmed && (nowMs - lastLeftClickMs >= CLICK_REARM_MS))
    {
      bleMouse.click(MOUSE_LEFT);
      lastLeftClickMs = nowMs;
      yawLeftArmed = false;
    }
  }
  else
  {
    yawLeftArmed = true; // re-arm once back inside threshold
  }

  if (yaw > YAW_CLICK_THRESHOLD_DEG)
  {
    if (yawRightArmed && (nowMs - lastRightClickMs >= CLICK_REARM_MS))
    {
      bleMouse.click(MOUSE_RIGHT);
      lastRightClickMs = nowMs;
      yawRightArmed = false;
    }
  }
  else
  {
    yawRightArmed = true;
  }

  // ── Cursor movement ───────────────────────────────────────────────────────

  const int moveX = constrain(static_cast<int>(roundf(roll * ANGLE_TO_MOUSE_GAIN)), -127, 127);
  const int moveY = constrain(static_cast<int>(roundf(-pitch * ANGLE_TO_MOUSE_GAIN)), -127, 127);

  if ((moveX != 0 || moveY != 0) && (nowMs - lastReportMs >= BLE_REPORT_INTERVAL_MS))
  {
    bleMouse.move(static_cast<int8_t>(moveX), static_cast<int8_t>(moveY));
    lastReportMs = nowMs;
  }

  if (nowMs - lastStatusMs > 1000)
  {
    Serial.print("yaw=");
    Serial.print(yaw, 2);
    Serial.print(" pitch=");
    Serial.print(pitch, 2);
    Serial.print(" roll=");
    Serial.println(roll, 2);
    lastStatusMs = nowMs;
  }
}