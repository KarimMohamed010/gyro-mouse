#include <Arduino.h>
#include <Adafruit_MPU6050.h>
#include <Adafruit_Sensor.h>
#include <Wire.h>
#include <BleMouse.h>
#include <BLEDevice.h>

Adafruit_MPU6050 mpu;
BleMouse bleMouse("ESP32 MPU Mouse", "Espressif", 100);

// ───────────── CONSTANTS ─────────────
constexpr float RAD_TO_DEG_F = 57.29577951308232f;
constexpr float GYRO_DEADBAND_DPS = 1.6f;
constexpr float GYRO_TO_MOUSE_GAIN = 0.3f;
constexpr float SMOOTHING_ALPHA = 0.28f;
constexpr uint16_t CALIBRATION_SAMPLES = 400;
constexpr uint16_t BLE_POST_CONNECT_DELAY_MS = 1200;
constexpr uint16_t BLE_REPORT_INTERVAL_MS = 16;

// ───────────── PINS ─────────────
constexpr uint8_t PIN_BTN_LEFT = 34;
constexpr uint8_t PIN_BTN_RIGHT = 35;
constexpr uint8_t PIN_BTN_GESTURE = 39; // Was scroll-down, now gesture modifier

// ───────────── BUTTON TIMING ─────────────
constexpr uint16_t BUTTON_DEBOUNCE_MS = 40;

// ───────────── GESTURE CONFIG ─────────────
// Minimum accumulated raw-DPS sum to count as a deliberate swipe
constexpr float GESTURE_SWIPE_THRESHOLD = 120.0f;
// Ratio test: dominant axis must be this many times larger than the other
constexpr float GESTURE_AXIS_RATIO = 1.4f;
// Max samples we record while gesture button is held (at ~10 ms each ≈ 1 s)
constexpr uint16_t GESTURE_MAX_SAMPLES = 100;
// How many direction reversals qualify as a "shake"
constexpr uint8_t SHAKE_REVERSAL_MIN = 4;
// Minimum movement to count a reversal segment
constexpr float SHAKE_SEG_THRESHOLD = 15.0f;

// ───────────── GESTURE BUFFER ─────────────
// We store raw DPS samples so we can do shape analysis (not just sums)
struct GestureSample
{
  float dpsX;
  float dpsY;
};

GestureSample gestureBuf[GESTURE_MAX_SAMPLES];
uint16_t gestureLen = 0;

// ───────────── CALIBRATION STATE ─────────────
float gyroBiasX = 0.0f;
float gyroBiasY = 0.0f;

// ───────────── FILTER STATE ─────────────
float filteredDpsX = 0.0f;
float filteredDpsY = 0.0f;

// ───────────── HELPERS ─────────────
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

// ───────────── GESTURE CLASSIFICATION ─────────────
enum GestureType
{
  GESTURE_NONE,
  GESTURE_SWIPE_LEFT,
  GESTURE_SWIPE_RIGHT,
  GESTURE_SWIPE_UP,
  GESTURE_SWIPE_DOWN,
  GESTURE_SHAKE_X,   // Rapid left-right shake
  GESTURE_SHAKE_Y,   // Rapid up-down shake
  GESTURE_DIAG_UR,   // Diagonal swipe upper-right
  GESTURE_DIAG_UL,   // Diagonal swipe upper-left
  GESTURE_DIAG_DR,   // Diagonal swipe down-right
  GESTURE_DIAG_DL,   // Diagonal swipe down-left
  GESTURE_TAP        // Button pressed with no significant movement
};

// Count how many times the sign of movement reverses on a given axis
uint8_t countReversals(bool useX)
{
  uint8_t reversals = 0;
  float segAccum = 0.0f;
  int8_t lastDir = 0; // -1 or +1

  for (uint16_t i = 0; i < gestureLen; i++)
  {
    float v = useX ? gestureBuf[i].dpsX : gestureBuf[i].dpsY;
    segAccum += v;

    if (fabsf(segAccum) >= SHAKE_SEG_THRESHOLD)
    {
      int8_t dir = (segAccum > 0) ? 1 : -1;
      if (lastDir != 0 && dir != lastDir)
      {
        reversals++;
      }
      lastDir = dir;
      segAccum = 0.0f;
    }
  }
  return reversals;
}

GestureType classifyGesture()
{
  if (gestureLen == 0)
  {
    return GESTURE_TAP;
  }

  // --- Compute aggregate sums ---
  float sumX = 0.0f;
  float sumY = 0.0f;
  for (uint16_t i = 0; i < gestureLen; i++)
  {
    sumX += gestureBuf[i].dpsX;
    sumY += gestureBuf[i].dpsY;
  }

  float absX = fabsf(sumX);
  float absY = fabsf(sumY);

  // --- Check for shakes first (override swipes) ---
  uint8_t revX = countReversals(true);
  uint8_t revY = countReversals(false);

  if (revX >= SHAKE_REVERSAL_MIN)
  {
    return GESTURE_SHAKE_X;
  }
  if (revY >= SHAKE_REVERSAL_MIN)
  {
    return GESTURE_SHAKE_Y;
  }

  // --- Not enough movement at all → tap ---
  if (absX < GESTURE_SWIPE_THRESHOLD && absY < GESTURE_SWIPE_THRESHOLD)
  {
    return GESTURE_TAP;
  }

  // --- Diagonal detection: both axes significant and ratio is close ---
  bool xSignificant = absX >= GESTURE_SWIPE_THRESHOLD;
  bool ySignificant = absY >= GESTURE_SWIPE_THRESHOLD;
  float ratio = (absX > absY) ? (absX / max(absY, 1.0f)) : (absY / max(absX, 1.0f));

  if (xSignificant && ySignificant && ratio < GESTURE_AXIS_RATIO)
  {
    // Diagonal
    if (sumX > 0 && sumY < 0)
      return GESTURE_DIAG_UR; // Right + Up (negative Y = up in our mapping)
    if (sumX < 0 && sumY < 0)
      return GESTURE_DIAG_UL;
    if (sumX > 0 && sumY > 0)
      return GESTURE_DIAG_DR;
    return GESTURE_DIAG_DL;
  }

  // --- Cardinal swipe ---
  if (absX > absY)
  {
    return (sumX > 0) ? GESTURE_SWIPE_RIGHT : GESTURE_SWIPE_LEFT;
  }
  else
  {
    // Note: positive filteredDpsY with negation in moveY means positive sumY = down
    return (sumY > 0) ? GESTURE_SWIPE_DOWN : GESTURE_SWIPE_UP;
  }
}

// ───────────── EXECUTE GESTURE ACTION ─────────────
void executeGesture(GestureType gesture)
{
  switch (gesture)
  {
  case GESTURE_SWIPE_RIGHT:
    Serial.println(">> SWIPE RIGHT → Browser Forward");
    bleMouse.click(MOUSE_FORWARD);
    break;

  case GESTURE_SWIPE_LEFT:
    Serial.println(">> SWIPE LEFT → Browser Back");
    bleMouse.click(MOUSE_BACK);
    break;

  case GESTURE_SWIPE_UP:
    Serial.println(">> SWIPE UP → Scroll Up (fast)");
    for (int i = 0; i < 5; i++)
    {
      bleMouse.move(0, 0, 1);
      delay(15);
    }
    break;

  case GESTURE_SWIPE_DOWN:
    Serial.println(">> SWIPE DOWN → Scroll Down (fast)");
    for (int i = 0; i < 5; i++)
    {
      bleMouse.move(0, 0, -1);
      delay(15);
    }
    break;

  case GESTURE_SHAKE_X:
    Serial.println(">> SHAKE X → Horizontal Scroll Left");
    for (int i = 0; i < 5; i++)
    {
      bleMouse.move(0, 0, 0, 1); // horizontal scroll left
      delay(15);
    }
    break;

  case GESTURE_SHAKE_Y:
    Serial.println(">> SHAKE Y → Horizontal Scroll Right");
    for (int i = 0; i < 5; i++)
    {
      bleMouse.move(0, 0, 0, -1); // horizontal scroll right
      delay(15);
    }
    break;

  case GESTURE_DIAG_UR:
    Serial.println(">> DIAGONAL UP-RIGHT → Double Click");
    bleMouse.click(MOUSE_LEFT);
    delay(60);
    bleMouse.click(MOUSE_LEFT);
    break;

  case GESTURE_DIAG_UL:
    Serial.println(">> DIAGONAL UP-LEFT → Middle Click (open in new tab)");
    bleMouse.click(MOUSE_MIDDLE);
    break;

  case GESTURE_DIAG_DR:
    Serial.println(">> DIAGONAL DOWN-RIGHT → Left+Right Click (context action)");
    bleMouse.click(MOUSE_LEFT | MOUSE_RIGHT);
    break;

  case GESTURE_DIAG_DL:
    Serial.println(">> DIAGONAL DOWN-LEFT → Middle Click");
    bleMouse.click(MOUSE_MIDDLE);
    break;

  case GESTURE_TAP:
    Serial.println(">> TAP (no movement) → Middle Click");
    bleMouse.click(MOUSE_MIDDLE);
    break;

  case GESTURE_NONE:
  default:
    Serial.println(">> No gesture detected");
    break;
  }
}

// ───────────── SETUP ─────────────
void setup(void)
{
  Serial.begin(115200);
  delay(200);

  // GPIO34 is input-only and needs an external pull-up or pull-down resistor.
  pinMode(PIN_BTN_LEFT, INPUT);
  pinMode(PIN_BTN_RIGHT, INPUT_PULLUP);
  pinMode(PIN_BTN_GESTURE, INPUT_PULLUP);

  Serial.println("ESP32 MPU BLE Mouse + Gestures starting...");

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
  Serial.println("Gesture button (GPIO39): hold + move to gesture, tap for middle-click");
  Serial.println("Gestures: Swipe L/R/U/D, Shake X/Y, Diagonals, Tap");
}

// ───────────── MAIN LOOP ─────────────
void loop()
{
  // --- Timing & state ---
  static uint32_t lastSampleMs = 0;
  static uint32_t lastStatusMs = 0;
  static uint32_t lastAdvertiseKickMs = 0;
  static uint32_t connectedSinceMs = 0;
  static uint32_t lastReportMs = 0;
  static uint32_t lastLeftClickMs = 0;
  static uint32_t lastRightClickMs = 0;

  static bool leftWasPressed = false;
  static bool rightWasPressed = false;
  static bool gestureWasPressed = false;
  static bool wasConnected = false;
  static bool gestureActive = false; // true while recording a gesture

  const uint32_t nowMs = millis();
  const bool connected = bleMouse.isConnected();

  // --- BLE connection management ---
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
    gestureActive = false;
    gestureLen = 0;
    Serial.println("BLE disconnected. Re-advertising for any host...");
  }

  wasConnected = connected;

  // --- 10 ms sample gate ---
  if (nowMs - lastSampleMs < 10)
  {
    return;
  }
  lastSampleMs = nowMs;

  // --- Not connected: keep advertising ---
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

  // --- Post-connect grace period ---
  if ((nowMs - connectedSinceMs) < BLE_POST_CONNECT_DELAY_MS)
  {
    if (nowMs - lastStatusMs > 1000)
    {
      Serial.println("BLE link up. Waiting for HID notifications to be ready...");
      lastStatusMs = nowMs;
    }
    return;
  }

  // --- Read buttons ---
  const bool leftPressed = (digitalRead(PIN_BTN_LEFT) == LOW);
  const bool rightPressed = (digitalRead(PIN_BTN_RIGHT) == LOW);
  const bool gesturePressed = (digitalRead(PIN_BTN_GESTURE) == LOW);

  // --- Left click (debounced) ---
  if (leftPressed && !leftWasPressed && (nowMs - lastLeftClickMs >= BUTTON_DEBOUNCE_MS))
  {
    bleMouse.click(MOUSE_LEFT);
    lastLeftClickMs = nowMs;
  }

  // --- Right click (debounced) ---
  if (rightPressed && !rightWasPressed && (nowMs - lastRightClickMs >= BUTTON_DEBOUNCE_MS))
  {
    bleMouse.click(MOUSE_RIGHT);
    lastRightClickMs = nowMs;
  }

  // --- Read gyro ---
  sensors_event_t a, g, temp;
  mpu.getEvent(&a, &g, &temp);

  float gyroXDps = (g.gyro.y - gyroBiasY) * RAD_TO_DEG_F;
  float gyroYDps = (g.gyro.x - gyroBiasX) * RAD_TO_DEG_F;

  gyroXDps = applyDeadband(gyroXDps, GYRO_DEADBAND_DPS);
  gyroYDps = applyDeadband(gyroYDps, GYRO_DEADBAND_DPS);

  // ─── GESTURE MODE ───
  if (gesturePressed)
  {
    // Just started holding → begin recording
    if (!gestureActive)
    {
      gestureActive = true;
      gestureLen = 0;
      // Reset the smoothing filter so cursor doesn't jump on release
      filteredDpsX = 0.0f;
      filteredDpsY = 0.0f;
    }

    // Record raw DPS sample into gesture buffer
    if (gestureLen < GESTURE_MAX_SAMPLES)
    {
      gestureBuf[gestureLen].dpsX = gyroXDps;
      gestureBuf[gestureLen].dpsY = gyroYDps;
      gestureLen++;
    }
    // Do NOT move the cursor while gesture is active
  }
  else if (gestureActive)
  {
    // Gesture button just released → classify and execute
    gestureActive = false;

    Serial.print("Gesture recorded: ");
    Serial.print(gestureLen);
    Serial.println(" samples");

    GestureType result = classifyGesture();
    executeGesture(result);

    gestureLen = 0;
  }
  else
  {
    // ─── NORMAL MOUSE MODE ───
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
  }

  // --- Status print ---
  if (nowMs - lastStatusMs > 1000)
  {
    if (gestureActive)
    {
      Serial.print("GESTURE MODE | samples=");
      Serial.println(gestureLen);
    }
    else
    {
      Serial.print("Connected | dpsX=");
      Serial.print(filteredDpsX, 2);
      Serial.print(" dpsY=");
      Serial.println(filteredDpsY, 2);
    }
    lastStatusMs = nowMs;
  }

  // --- Update previous-frame button states (must be LAST) ---
  leftWasPressed = leftPressed;
  rightWasPressed = rightPressed;
  gestureWasPressed = gesturePressed;
}