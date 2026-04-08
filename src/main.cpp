#include <Arduino.h>
#include <Adafruit_MPU6050.h>
#include <Adafruit_Sensor.h>
#include <Wire.h>
#include <BleMouse.h>
#include <BLEDevice.h>

Adafruit_MPU6050 mpu;
BleMouse bleMouse("ESP32 MPU Mouse", "Espressif", 100);

constexpr float RAD_TO_DEG_F = 57.29577951308232f;
constexpr float GYRO_DEADBAND_DPS = 1.6f;
constexpr float GYRO_TO_MOUSE_GAIN = 0.14f;
constexpr float SMOOTHING_ALPHA = 0.28f;
constexpr uint16_t CALIBRATION_SAMPLES = 400;
constexpr uint16_t BLE_POST_CONNECT_DELAY_MS = 1200;
constexpr uint16_t BLE_REPORT_INTERVAL_MS = 16;
constexpr uint8_t PIN_BTN_LEFT = 34;
constexpr uint8_t PIN_BTN_RIGHT = 35;
constexpr uint8_t PIN_BTN_SCROLL_DOWN = 33;
constexpr uint16_t BUTTON_DEBOUNCE_MS = 40;
constexpr uint16_t SCROLL_REPEAT_MS = 120;

float gyroBiasX = 0.0f;
float gyroBiasY = 0.0f;
float filteredDpsX = 0.0f;
float filteredDpsY = 0.0f;

float applyDeadband(float value, float deadband)
{
  if (fabsf(value) < deadband)
  {
    return 0.0f;
  }
  return value;
}

void calibrateGyro()
{
  float sumX = 0.0f;
  float sumY = 0.0f;

  Serial.println("Calibrating gyro. Keep MPU6050 still...");
  for (uint16_t i = 0; i < CALIBRATION_SAMPLES; ++i)
  {
    sensors_event_t a, g, temp;
    mpu.getEvent(&a, &g, &temp);
    sumX += g.gyro.x;
    sumY += g.gyro.y;
    delay(4);
  }

  gyroBiasX = sumX / CALIBRATION_SAMPLES;
  gyroBiasY = sumY / CALIBRATION_SAMPLES;
  Serial.println("Gyro calibration complete.");
}

void setup(void)
{
  Serial.begin(115200);
  delay(200);

  // GPIO34 is input-only and needs an external pull-up or pull-down resistor.
  pinMode(PIN_BTN_LEFT, INPUT);
  pinMode(PIN_BTN_RIGHT, INPUT_PULLUP);
  pinMode(PIN_BTN_SCROLL_DOWN, INPUT_PULLUP);

  Serial.println("ESP32 MPU BLE Mouse starting...");

  if (!mpu.begin())
  {
    Serial.println("Failed to find MPU6050 chip");
    while (true)
    {
      delay(10);
    }
  }

  mpu.setAccelerometerRange(MPU6050_RANGE_4_G);
  mpu.setGyroRange(MPU6050_RANGE_500_DEG);
  mpu.setFilterBandwidth(MPU6050_BAND_21_HZ);

  calibrateGyro();

  bleMouse.begin();
  Serial.println("Pair to device: ESP32 MPU Mouse");
}

void loop()
{
  static uint32_t lastSampleMs = 0;
  static uint32_t lastStatusMs = 0;
  static uint32_t lastAdvertiseKickMs = 0;
  static uint32_t connectedSinceMs = 0;
  static uint32_t lastReportMs = 0;
  static uint32_t lastLeftClickMs = 0;
  static uint32_t lastRightClickMs = 0;
  static uint32_t lastScrollStepMs = 0;
  static bool leftWasPressed = false;
  static bool rightWasPressed = false;
  static bool scrollWasPressed = false;
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
    filteredDpsX = 0.0f;
    filteredDpsY = 0.0f;
    connectedSinceMs = 0;
    lastReportMs = 0;
    lastAdvertiseKickMs = nowMs;
    Serial.println("BLE disconnected. Re-advertising for any host...");
  }

  wasConnected = connected;

  if (nowMs - lastSampleMs < 10)
  {
    return;
  }
  lastSampleMs = nowMs;

  if (!connected)
  {
    if (nowMs - lastAdvertiseKickMs > 5000)
    {
      BLEDevice::startAdvertising();
      lastAdvertiseKickMs = nowMs;
    }

    if (nowMs - lastStatusMs > 1000)
    {
      Serial.println("Waiting for BLE mouse connection...");
      lastStatusMs = nowMs;
    }
    return;
  }

  if ((nowMs - connectedSinceMs) < BLE_POST_CONNECT_DELAY_MS)
  {
    if (nowMs - lastStatusMs > 1000)
    {
      Serial.println("BLE link up. Waiting for HID notifications to be ready...");
      lastStatusMs = nowMs;
    }
    return;
  }

  const bool leftPressed = (digitalRead(PIN_BTN_LEFT) == LOW);
  const bool rightPressed = (digitalRead(PIN_BTN_RIGHT) == LOW);
  const bool scrollPressed = (digitalRead(PIN_BTN_SCROLL_DOWN) == LOW);

  if (leftPressed && !leftWasPressed && (nowMs - lastLeftClickMs >= BUTTON_DEBOUNCE_MS))
  {
    bleMouse.click(MOUSE_LEFT);
    lastLeftClickMs = nowMs;
  }

  if (rightPressed && !rightWasPressed && (nowMs - lastRightClickMs >= BUTTON_DEBOUNCE_MS))
  {
    bleMouse.click(MOUSE_RIGHT);
    lastRightClickMs = nowMs;
  }

  if (scrollPressed)
  {
    const uint32_t repeatMs = scrollWasPressed ? SCROLL_REPEAT_MS : BUTTON_DEBOUNCE_MS;
    if (nowMs - lastScrollStepMs >= repeatMs)
    {
      bleMouse.move(0, 0, -1);
      lastScrollStepMs = nowMs;
    }
  }

  leftWasPressed = leftPressed;
  rightWasPressed = rightPressed;
  scrollWasPressed = scrollPressed;

  sensors_event_t a, g, temp;
  mpu.getEvent(&a, &g, &temp);

  float gyroXDps = (g.gyro.y - gyroBiasY) * RAD_TO_DEG_F;
  float gyroYDps = (g.gyro.x - gyroBiasX) * RAD_TO_DEG_F;

  gyroXDps = applyDeadband(gyroXDps, GYRO_DEADBAND_DPS);
  gyroYDps = applyDeadband(gyroYDps, GYRO_DEADBAND_DPS);

  filteredDpsX = (1.0f - SMOOTHING_ALPHA) * filteredDpsX + SMOOTHING_ALPHA * gyroXDps;
  filteredDpsY = (1.0f - SMOOTHING_ALPHA) * filteredDpsY + SMOOTHING_ALPHA * gyroYDps;

  int moveX = static_cast<int>(roundf(filteredDpsX * GYRO_TO_MOUSE_GAIN));
  int moveY = static_cast<int>(roundf(-filteredDpsY * GYRO_TO_MOUSE_GAIN));

  moveX = constrain(moveX, -127, 127);
  moveY = constrain(moveY, -127, 127);

  if ((moveX != 0 || moveY != 0) && (nowMs - lastReportMs >= BLE_REPORT_INTERVAL_MS))
  {
    bleMouse.move(static_cast<int8_t>(moveX), static_cast<int8_t>(moveY));
    lastReportMs = nowMs;
  }

  if (nowMs - lastStatusMs > 1000)
  {
    Serial.print("Connected | dpsX=");
    Serial.print(filteredDpsX, 2);
    Serial.print(" dpsY=");
    Serial.println(filteredDpsY, 2);
    lastStatusMs = nowMs;
  }
}