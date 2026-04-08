#include <Arduino.h>
#include <Adafruit_MPU6050.h>
#include <Adafruit_Sensor.h>
#include <Wire.h>

Adafruit_MPU6050 mpu;

void setup(void)
{
  Serial.begin(115200);
  while (!Serial)
    delay(10); // will pause Zero, Leonardo, etc until serial console opens

  Serial.println("Adafruit MPU6050 test!");

  // Try to initialize!
  if (!mpu.begin())
  {
    Serial.println("Failed to find MPU6050 chip");
    while (1)
    {
      delay(10);
    }
  }
  Serial.println("MPU6050 Found!");

  // setupt motion detection
  mpu.setHighPassFilter(MPU6050_HIGHPASS_0_63_HZ);
  mpu.setMotionDetectionThreshold(1);
  mpu.setMotionDetectionDuration(20);
  mpu.setInterruptPinLatch(true); // Keep it latched.  Will turn off when reinitialized.
  mpu.setInterruptPinPolarity(true);
  mpu.setMotionInterrupt(true);

  Serial.println("");
  delay(100);
}

void loop()
{

  if (mpu.getMotionInterruptStatus())
  {
    /* Get new sensor events with the readings */
    sensors_event_t a, g, temp;
    mpu.getEvent(&a, &g, &temp);

    /* Compute orientation (roll, pitch) from accelerometer and convert to degrees */
    constexpr float RAD_TO_DEG_F = 57.29577951308232f;
    float roll = atan2(a.acceleration.y, a.acceleration.z) * RAD_TO_DEG_F;
    float pitch = atan2(-a.acceleration.x, sqrt(a.acceleration.y * a.acceleration.y + a.acceleration.z * a.acceleration.z)) * RAD_TO_DEG_F;

    /* Convert gyro (rad/s) to degrees/s for easier reading */
    float gyroX_deg = g.gyro.x * RAD_TO_DEG_F;
    float gyroY_deg = g.gyro.y * RAD_TO_DEG_F;
    float gyroZ_deg = g.gyro.z * RAD_TO_DEG_F;

    /* Print out the values in a clearer, degree-based format */
    Serial.print("AccelX:");
    Serial.print(a.acceleration.x);
    Serial.print(", ");
    Serial.print("AccelY:");
    Serial.print(a.acceleration.y);
    Serial.print(", ");
    Serial.print("AccelZ:");
    Serial.print(a.acceleration.z);
    Serial.println();

    Serial.print("Roll(deg):");
    Serial.print(roll);
    Serial.print(", ");
    Serial.print("Pitch(deg):");
    Serial.print(pitch);
    Serial.println();

    Serial.print("GyroX(deg/s):");
    Serial.print(gyroX_deg);
    Serial.print(", ");
    Serial.print("GyroY(deg/s):");
    Serial.print(gyroY_deg);
    Serial.print(", ");
    Serial.print("GyroZ(deg/s):");
    Serial.print(gyroZ_deg);
    Serial.println();
  }

  delay(10);
}