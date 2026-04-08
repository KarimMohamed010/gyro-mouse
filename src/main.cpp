#include <Arduino.h>
#include <Adafruit_MPU6050.h>
#include <Adafruit_Sensor.h>
#include <Wire.h>
#include <BleMouse.h>
#include <BLEDevice.h>

Adafruit_MPU6050 mpu;
BleMouse bleMouse("ESP32 MPU Mouse", "Espressif", 100);

constexpr float RAD_TO_DEG_F = 57.29577951308232f;
constexpr float GYRO_DEADBAND_DPS = 0.7f;
constexpr float COMPLEMENTARY_ALPHA = 0.97f;
constexpr float ANGLE_DEADBAND_DEG = 1.3f;
constexpr float ANGLE_TO_MOUSE_GAIN = 2.8f;
constexpr float CLICK_YAW_THRESHOLD_DEG = 14.0f;
constexpr float CLICK_YAW_RELEASE_DEG = 6.0f;
constexpr uint16_t CALIBRATION_SAMPLES = 400;
constexpr uint16_t BLE_POST_CONNECT_DELAY_MS = 1200;
constexpr uint16_t BLE_REPORT_INTERVAL_MS = 16;
constexpr uint16_t SAMPLE_INTERVAL_MS = 10;
constexpr uint8_t PIN_BTN_AXIS_RESET = 39;
constexpr uint16_t BUTTON_DEBOUNCE_MS = 40;
constexpr uint16_t CLICK_COOLDOWN_MS = 220;

float gyroBiasX = 0.0f;
float gyroBiasY = 0.0f;
float gyroBiasZ = 0.0f;

float fusedPitchDeg = 0.0f;
float fusedRollDeg = 0.0f;
float fusedYawDeg = 0.0f;

float neutralPitchDeg = 0.0f;
float neutralRollDeg = 0.0f;
float neutralYawDeg = 0.0f;

float applyDeadband(float value, float deadband)
{
  if (fabsf(value) < deadband)
  {
    return 0.0f;
  }
  return value;
}

float accelToPitchDeg(const sensors_event_t &accelEvent)
{
  const float ax = accelEvent.acceleration.x;
  const float ay = accelEvent.acceleration.y;
  const float az = accelEvent.acceleration.z;
  return atan2f(-ax, sqrtf((ay * ay) + (az * az))) * RAD_TO_DEG_F;
}

float accelToRollDeg(const sensors_event_t &accelEvent)
{
  const float ay = accelEvent.acceleration.y;
  const float az = accelEvent.acceleration.z;
  return atan2f(ay, az) * RAD_TO_DEG_F;
}

void captureNeutralPose()
{
  neutralPitchDeg = fusedPitchDeg;
  neutralRollDeg = fusedRollDeg;
  neutralYawDeg = fusedYawDeg;
}

void calibrateGyroAndPose()
{
  float sumX = 0.0f;
  float sumY = 0.0f;
  float sumZ = 0.0f;
  float sumPitch = 0.0f;
  float sumRoll = 0.0f;

  Serial.println("Calibrating gyro. Keep MPU6050 still...");
  for (uint16_t i = 0; i < CALIBRATION_SAMPLES; ++i)
  {
    sensors_event_t a, g, temp;
    mpu.getEvent(&a, &g, &temp);
    sumX += g.gyro.x;
    sumY += g.gyro.y;
    sumZ += g.gyro.z;
    sumPitch += accelToPitchDeg(a);
    sumRoll += accelToRollDeg(a);
    delay(4);
  }

  gyroBiasX = sumX / CALIBRATION_SAMPLES;
  gyroBiasY = sumY / CALIBRATION_SAMPLES;
  gyroBiasZ = sumZ / CALIBRATION_SAMPLES;

  fusedPitchDeg = sumPitch / CALIBRATION_SAMPLES;
  fusedRollDeg = sumRoll / CALIBRATION_SAMPLES;
  fusedYawDeg = 0.0f;
  captureNeutralPose();

  Serial.println("Gyro calibration complete. Neutral pose saved.");
}

void setup(void)
{
  Serial.begin(115200);
  delay(200);

  // GPIO39 is input-only; use external pull-up/pull-down in hardware.
  pinMode(PIN_BTN_AXIS_RESET, INPUT);

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

  calibrateGyroAndPose();

  bleMouse.begin();
  Serial.println("Pair to device: ESP32 MPU Mouse");
  Serial.println("Head controls: pitch/roll = cursor, yaw = left/right click, button = recenter.");
}

void loop()
{
  static uint32_t lastSampleMs = 0;
  static uint32_t lastStatusMs = 0;
  static uint32_t lastAdvertiseKickMs = 0;
  static uint32_t connectedSinceMs = 0;
  static uint32_t lastReportMs = 0;
  static uint32_t lastClickMs = 0;
  static uint32_t lastAxisResetMs = 0;
  static bool axisResetWasPressed = false;
  static bool yawClickLatched = false;
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
    connectedSinceMs = 0;
    lastReportMs = 0;
    yawClickLatched = false;
    lastAdvertiseKickMs = nowMs;
    Serial.println("BLE disconnected. Re-advertising for any host...");
  }

  wasConnected = connected;

  if (lastSampleMs == 0)
  {
    lastSampleMs = nowMs;
    return;
  }

  const uint32_t deltaSampleMs = nowMs - lastSampleMs;
  if (deltaSampleMs < SAMPLE_INTERVAL_MS)
  {
    return;
  }

  const float dtSec = static_cast<float>(deltaSampleMs) * 0.001f;
  lastSampleMs = nowMs;

  sensors_event_t a, g, temp;
  mpu.getEvent(&a, &g, &temp);

  float gyroPitchDps = (g.gyro.x - gyroBiasX) * RAD_TO_DEG_F;
  float gyroRollDps = (g.gyro.y - gyroBiasY) * RAD_TO_DEG_F;
  float gyroYawDps = (g.gyro.z - gyroBiasZ) * RAD_TO_DEG_F;

  gyroPitchDps = applyDeadband(gyroPitchDps, GYRO_DEADBAND_DPS);
  gyroRollDps = applyDeadband(gyroRollDps, GYRO_DEADBAND_DPS);
  gyroYawDps = applyDeadband(gyroYawDps, GYRO_DEADBAND_DPS);

  const float accelPitchDeg = accelToPitchDeg(a);
  const float accelRollDeg = accelToRollDeg(a);

  fusedPitchDeg = COMPLEMENTARY_ALPHA * (fusedPitchDeg + (gyroPitchDps * dtSec)) + (1.0f - COMPLEMENTARY_ALPHA) * accelPitchDeg;
  fusedRollDeg = COMPLEMENTARY_ALPHA * (fusedRollDeg + (gyroRollDps * dtSec)) + (1.0f - COMPLEMENTARY_ALPHA) * accelRollDeg;
  fusedYawDeg += gyroYawDps * dtSec;

  const bool axisResetPressed = (digitalRead(PIN_BTN_AXIS_RESET) == LOW);
  if (axisResetPressed && !axisResetWasPressed && (nowMs - lastAxisResetMs >= BUTTON_DEBOUNCE_MS))
  {
    captureNeutralPose();
    yawClickLatched = false;
    lastAxisResetMs = nowMs;
    Serial.println("Neutral pose reset from button.");
  }
  axisResetWasPressed = axisResetPressed;

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

  const float relRollDeg = applyDeadband(fusedRollDeg - neutralRollDeg, ANGLE_DEADBAND_DEG);
  const float relPitchDeg = applyDeadband(fusedPitchDeg - neutralPitchDeg, ANGLE_DEADBAND_DEG);
  const float relYawDeg = fusedYawDeg - neutralYawDeg;

  int moveX = static_cast<int>(roundf(relRollDeg * ANGLE_TO_MOUSE_GAIN));
  int moveY = static_cast<int>(roundf(-relPitchDeg * ANGLE_TO_MOUSE_GAIN));

  moveX = constrain(moveX, -127, 127);
  moveY = constrain(moveY, -127, 127);

  if (!yawClickLatched && (nowMs - lastClickMs >= CLICK_COOLDOWN_MS))
  {
    if (relYawDeg <= -CLICK_YAW_THRESHOLD_DEG)
    {
      bleMouse.click(MOUSE_LEFT);
      yawClickLatched = true;
      lastClickMs = nowMs;
    }
    else if (relYawDeg >= CLICK_YAW_THRESHOLD_DEG)
    {
      bleMouse.click(MOUSE_RIGHT);
      yawClickLatched = true;
      lastClickMs = nowMs;
    }
  }

  if (fabsf(relYawDeg) < CLICK_YAW_RELEASE_DEG)
  {
    yawClickLatched = false;
  }

  if ((moveX != 0 || moveY != 0) && (nowMs - lastReportMs >= BLE_REPORT_INTERVAL_MS))
  {
    bleMouse.move(static_cast<int8_t>(moveX), static_cast<int8_t>(moveY));
    lastReportMs = nowMs;
  }

  if (nowMs - lastStatusMs > 1000)
  {
    Serial.print("Connected | pitch=");
    Serial.print(fusedPitchDeg - neutralPitchDeg, 1);
    Serial.print(" roll=");
    Serial.print(fusedRollDeg - neutralRollDeg, 1);
    Serial.print(" yaw=");
    Serial.println(relYawDeg, 1);
    lastStatusMs = nowMs;
  }
}
