#include <Arduino.h>
#include <Adafruit_MPU6050.h>
#include <Adafruit_Sensor.h>
#include <Wire.h>
#include <BleMouse.h>
#include <BLEDevice.h>
#include <math.h>

// ==================== OBJECTS ====================
Adafruit_MPU6050 mpu;
BleMouse bleMouse("ESP32 MPU Mouse", "Espressif", 100);

// ==================== PINS ====================
constexpr uint8_t  PIN_BTN_LEFT         = 34;
constexpr uint8_t  PIN_BTN_RIGHT        = 35;
constexpr uint8_t  PIN_BTN_SCROLL_DOWN  = 39;

// ==================== BLE MOUSE CONSTANTS ====================
constexpr float    GYRO_DEADBAND_DPS    = 1.6f;
constexpr float    GYRO_TO_MOUSE_GAIN   = 0.3f;
constexpr float    SMOOTHING_ALPHA      = 0.28f;
constexpr uint16_t CALIBRATION_SAMPLES  = 400;
constexpr uint16_t BLE_POST_CONNECT_DELAY = 1200;
constexpr uint16_t BLE_REPORT_INTERVAL_MS = 16;
constexpr uint16_t BUTTON_DEBOUNCE_MS   = 40;
constexpr uint16_t SCROLL_REPEAT_MS     = 120;

// ==================== GESTURE CONSTANTS ====================
constexpr float    RAD_TO_DEG_F           = 57.2958f;
constexpr float    MOTION_START_THRESHOLD = 1.5f;
constexpr float    GESTURE_FAST_TRIGGER   = 4.5f;   // = MOTION_START_THRESHOLD × 3
constexpr int      MAX_LEN                = 100;
constexpr int      TRAIN_SAMPLES          = 30;
constexpr int      NUM_GESTURES           = 2;

// ==================== GESTURE STORAGE ====================
float refGesture[NUM_GESTURES][MAX_LEN][2];
int   refLen    [NUM_GESTURES];
float refSumX   [NUM_GESTURES];
float refSumY   [NUM_GESTURES];
float threshold [NUM_GESTURES];

// gesture Actions 
// [0] = MOUSE_LEFT   [1] = MOUSE_RIGHT
const uint8_t gestureAction[NUM_GESTURES] = { MOUSE_LEFT, MOUSE_RIGHT };
const char*   gestureName  [NUM_GESTURES] = { "LEFT-CLICK", "RIGHT-CLICK" };

// ==================== TEMP BUFFER ====================
float testGesture[MAX_LEN][2];
int   testLen = 0;
float testSumX, testSumY;

// ==================== SHARED BIAS ====================
float gyroBiasX = 0.0f;
float gyroBiasY = 0.0f;

// ==================== MOUSE FILTER ====================
float filteredDpsX = 0.0f;
float filteredDpsY = 0.0f;

// ================================================================
//  HELPERS
// ================================================================
float applyDeadband(float v, float db) { return fabsf(v) < db ? 0.0f : v; }

String getDirection(float sx, float sy)
{
  if (fabsf(sx) > fabsf(sy)) return sx > 0 ? "RIGHT" : "LEFT";
  return sy > 0 ? "UP" : "DOWN";
}

// ================================================================
//  CALIBRATION
// ================================================================
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

// ================================================================
//  DTW
// ================================================================
float dtw(float a[][2], int lenA, float b[][2], int lenB)
{
  static float dp[MAX_LEN][MAX_LEN];
  for (int i = 0; i < lenA; i++)
    for (int j = 0; j < lenB; j++)
      dp[i][j] = 1e9f;

  dp[0][0] = hypot(a[0][0]-b[0][0], a[0][1]-b[0][1]);
  for (int i = 0; i < lenA; i++)
    for (int j = 0; j < lenB; j++)
    {
      float cost = hypot(a[i][0]-b[j][0], a[i][1]-b[j][1]);
      if (i > 0)           dp[i][j] = min(dp[i][j], dp[i-1][j]   + cost);
      if (j > 0)           dp[i][j] = min(dp[i][j], dp[i][j-1]   + cost);
      if (i > 0 && j > 0) dp[i][j] = min(dp[i][j], dp[i-1][j-1] + cost);
    }
  return dp[lenA-1][lenB-1];
}

// ================================================================
//  RECORD GESTURE
// ================================================================
int recordGesture(float buffer[][2])
{
  int  len = 0;
  bool rec = false;
  int  still = 0;

  while (true)
  {
    sensors_event_t a, g, t;
    mpu.getEvent(&a, &g, &t);
    float gx     = (g.gyro.x - gyroBiasX) * RAD_TO_DEG_F;
    float gy     = (g.gyro.y - gyroBiasY) * RAD_TO_DEG_F;
    float motion = sqrtf(gx*gx + gy*gy);

    if (!rec && motion > MOTION_START_THRESHOLD) { rec = true; len = still = 0; }

    if (rec)
    {
      if (len < MAX_LEN)
      {
        buffer[len][0] = gx;
        buffer[len][1] = gy;
        len++;
        still = (motion < 1.0f) ? still+1 : 0;
        if (still > 20) break;
      }
      else break;
    }
    delay(10);
  }
  return len;
}

// ================================================================
//  TRAIN ONE GESTURE
// ================================================================
void trainOne(int idx)
{
  float scores[TRAIN_SAMPLES];
  Serial.printf("\n-- Training gesture %d (%s) --\n", idx+1, gestureName[idx]);

  for (int i = 0; i < TRAIN_SAMPLES; i++)
  {
    Serial.printf("Sample %d/%d\n", i+1, TRAIN_SAMPLES);
    testLen = recordGesture(testGesture);

    float score = dtw(refGesture[idx], refLen[idx], testGesture, testLen);
    score /= max(refLen[idx], testLen);
    scores[i] = score;
    Serial.printf("  score=%.3f\n", score);
    delay(1500);
  }

  // Sort → 90th percentile
  for (int i = 0; i < TRAIN_SAMPLES-1; i++)
    for (int j = i+1; j < TRAIN_SAMPLES; j++)
      if (scores[j] < scores[i]) { float tmp=scores[i]; scores[i]=scores[j]; scores[j]=tmp; }

  threshold[idx] = scores[(int)(0.9f * TRAIN_SAMPLES)];
  Serial.printf("Threshold[%d] = %.3f\n", idx, threshold[idx]);
}

// ================================================================
//  GESTURE ACTION
// ================================================================
void onGestureRecognised(int idx)
{
  if (!bleMouse.isConnected()) return;
  Serial.printf(">>> %s\n", gestureName[idx]);
  bleMouse.click(gestureAction[idx]);
}

// ================================================================
//  SETUP
// ================================================================
void setup()
{
  Serial.begin(115200);
  delay(500);

  pinMode(PIN_BTN_LEFT,        INPUT);
  pinMode(PIN_BTN_RIGHT,       INPUT_PULLUP);
  pinMode(PIN_BTN_SCROLL_DOWN, INPUT_PULLUP);

  Serial.println("ESP32 MPU BLE Mouse starting...");

  if (!mpu.begin()) { Serial.println("MPU not found!"); while(true) delay(10); }
  mpu.setAccelerometerRange(MPU6050_RANGE_4_G);
  mpu.setGyroRange(MPU6050_RANGE_500_DEG);
  mpu.setFilterBandwidth(MPU6050_BAND_21_HZ);

  calibrateGyro();

  // ---- Record 2 references ----
  for (int i = 0; i < NUM_GESTURES; i++)
  {
    Serial.printf("\nRecord REFERENCE for gesture %d (%s) – perform now...\n",
                  i+1, gestureName[i]);
    refLen[i] = recordGesture(refGesture[i]);

    refSumX[i] = refSumY[i] = 0;
    for (int k = 0; k < refLen[i]; k++)
    {
      refSumX[i] += refGesture[i][k][0];
      refSumY[i] += refGesture[i][k][1];
    }
    Serial.printf("Reference %d recorded. Dir: %s\n",
                  i+1, getDirection(refSumX[i], refSumY[i]).c_str());
    delay(2000);
  }

  // ---- Train thresholds ----
  for (int i = 0; i < NUM_GESTURES; i++) trainOne(i);

  // ---- BLE ----
  bleMouse.begin();
  Serial.println("\nBLE started – pair to: ESP32 MPU Mouse");
}

// ================================================================
//  LOOP
// ================================================================
void loop()
{
  static uint32_t lastSampleMs        = 0;
  static uint32_t lastStatusMs        = 0;
  static uint32_t lastAdvertiseKickMs = 0;
  static uint32_t connectedSinceMs    = 0;
  static uint32_t lastReportMs        = 0;
  static uint32_t lastLeftClickMs     = 0;
  static uint32_t lastRightClickMs    = 0;
  static uint32_t lastScrollStepMs    = 0;
  static bool leftWasPressed          = false;
  static bool rightWasPressed         = false;
  static bool scrollWasPressed        = false;
  static bool wasConnected            = false;

  const uint32_t nowMs     = millis();
  const bool     connected = bleMouse.isConnected();

  if (connected && !wasConnected)  { connectedSinceMs = lastReportMs = nowMs; Serial.println("BLE connected."); }
  if (!connected && wasConnected)
  {
    BLEDevice::startAdvertising();
    filteredDpsX = filteredDpsY = 0.0f;
    connectedSinceMs = lastReportMs = 0;
    lastAdvertiseKickMs = nowMs;
    Serial.println("BLE disconnected – re-advertising...");
  }
  wasConnected = connected;

  if (nowMs - lastSampleMs < 10) return;
  lastSampleMs = nowMs;

  if (!connected)
  {
    if (nowMs - lastAdvertiseKickMs > 5000) { BLEDevice::startAdvertising(); lastAdvertiseKickMs = nowMs; }
    if (nowMs - lastStatusMs        > 1000) { Serial.println("Waiting for BLE..."); lastStatusMs = nowMs; }
    return;
  }

  if ((nowMs - connectedSinceMs) < BLE_POST_CONNECT_DELAY)
  {
    if (nowMs - lastStatusMs > 1000) {       Serial.println("BLE link up. Waiting for HID notifications to be ready...");
     lastStatusMs = nowMs; }
    return;
  }

  // ---- Read IMU ----
  sensors_event_t a, g, t;
  mpu.getEvent(&a, &g, &t);
  float rawGx = (g.gyro.x - gyroBiasX) * RAD_TO_DEG_F;
  float rawGy = (g.gyro.y - gyroBiasY) * RAD_TO_DEG_F;
  float motion = sqrtf(rawGx*rawGx + rawGy*rawGy);

  // ================================================================
  //  GESTURE DETECTION
  // ================================================================
  if (motion > GESTURE_FAST_TRIGGER)
  {
    Serial.println("[GESTURE] Fast motion – recording...");
    testLen  = recordGesture(testGesture);
    testSumX = testSumY = 0;
    for (int k = 0; k < testLen; k++) { testSumX += testGesture[k][0]; testSumY += testGesture[k][1]; }

    String testDir = getDirection(testSumX, testSumY);

    // Try all gestures and pick the best (lowest) score
    int   bestIdx   = -1;
    float bestScore = 1e9f;

    for (int i = 0; i < NUM_GESTURES; i++)
    {
      // Direction check first (fast filter before DTW)
      if (getDirection(refSumX[i], refSumY[i]) != testDir) continue;

      float score = dtw(refGesture[i], refLen[i], testGesture, testLen);
      score /= max(refLen[i], testLen);

      Serial.printf("  vs gesture%d score=%.3f thr=%.3f\n", i+1, score, threshold[i]);

      if (score < threshold[i] && score < bestScore)
      {
        bestScore = score;
        bestIdx   = i;
      }
    }

    if (bestIdx >= 0)
      onGestureRecognised(bestIdx);
    else
      Serial.println("[GESTURE] No match.");

    filteredDpsX = filteredDpsY = 0;
    lastSampleMs = millis();
    return;
  }

  // ================================================================
  //  PHYSICAL BUTTONS
  // ================================================================
  bool leftPressed   = (digitalRead(PIN_BTN_LEFT)        == LOW);
  bool rightPressed  = (digitalRead(PIN_BTN_RIGHT)       == LOW);
  bool scrollPressed = (digitalRead(PIN_BTN_SCROLL_DOWN) == LOW);

  if (leftPressed  && !leftWasPressed  && nowMs - lastLeftClickMs  >= BUTTON_DEBOUNCE_MS)
    { bleMouse.click(MOUSE_LEFT);  lastLeftClickMs  = nowMs; }
  if (rightPressed && !rightWasPressed && nowMs - lastRightClickMs >= BUTTON_DEBOUNCE_MS)
    { bleMouse.click(MOUSE_RIGHT); lastRightClickMs = nowMs; }
  if (scrollPressed)
  {
    uint32_t rep = scrollWasPressed ? SCROLL_REPEAT_MS : BUTTON_DEBOUNCE_MS;
    if (nowMs - lastScrollStepMs >= rep) { bleMouse.move(0,0,-1); lastScrollStepMs = nowMs; }
  }

  leftWasPressed   = leftPressed;
  rightWasPressed  = rightPressed;
  scrollWasPressed = scrollPressed;

  // ================================================================
  //  MOUSE MOVEMENT
  // ================================================================
  float mx = applyDeadband((g.gyro.y - gyroBiasY) * RAD_TO_DEG_F, GYRO_DEADBAND_DPS);
  float my = applyDeadband((g.gyro.x - gyroBiasX) * RAD_TO_DEG_F, GYRO_DEADBAND_DPS);

  filteredDpsX = (1.0f-SMOOTHING_ALPHA)*filteredDpsX + SMOOTHING_ALPHA*mx;
  filteredDpsY = (1.0f-SMOOTHING_ALPHA)*filteredDpsY + SMOOTHING_ALPHA*my;

  int moveX = constrain((int)roundf( filteredDpsX * GYRO_TO_MOUSE_GAIN), -127, 127);
  int moveY = constrain((int)roundf(-filteredDpsY * GYRO_TO_MOUSE_GAIN), -127, 127);

  if ((moveX || moveY) && nowMs - lastReportMs >= BLE_REPORT_INTERVAL_MS)
    { bleMouse.move((int8_t)moveX, (int8_t)moveY); lastReportMs = nowMs; }

  // ================================================================
  //  STATUS LOG
  // ================================================================
  if (nowMs - lastStatusMs > 1000)
    { Serial.printf("Mouse dpsX=%.2f dpsY=%.2f\n", filteredDpsX, filteredDpsY); lastStatusMs = nowMs; }
}