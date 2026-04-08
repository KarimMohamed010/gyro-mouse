#include <Arduino.h>
#include <Adafruit_MPU6050.h>
#include <Adafruit_Sensor.h>
#include <Wire.h>
#include <BleMouse.h>

Adafruit_MPU6050 mpu;
BleMouse bleMouse("ESP32 MPU Mouse", "Espressif", 100);

constexpr float RAD_TO_DEG_F = 57.29577951308232f;
constexpr float GYRO_DEADBAND_DPS = 1.6f;
constexpr float GYRO_TO_MOUSE_GAIN = 0.14f;
constexpr float SMOOTHING_ALPHA = 0.28f;
constexpr uint16_t CALIBRATION_SAMPLES = 400;

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
  const uint32_t nowMs = millis();

  if (nowMs - lastSampleMs < 10)
  {
    return;
  }
  lastSampleMs = nowMs;

  if (!bleMouse.isConnected())
  {
    if (nowMs - lastStatusMs > 1000)
    {
      Serial.println("Waiting for BLE mouse connection...");
      lastStatusMs = nowMs;
    }
    return;
  }

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

  if (moveX != 0 || moveY != 0)
  {
    bleMouse.move(static_cast<int8_t>(moveX), static_cast<int8_t>(moveY));
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