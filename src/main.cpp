#include <Arduino.h>
#include <Adafruit_MPU6050.h>
#include <Adafruit_Sensor.h>
#include <Wire.h>
#include <BleMouse.h>
#include <BLEDevice.h>
#include <Preferences.h>
#include <vector>

Adafruit_MPU6050 mpu;
BleMouse bleMouse("ESP32 MPU Mouse", "Espressif", 100);
Preferences preferences;

constexpr float RAD_TO_DEG_F = 57.29577951308232f;
constexpr float GYRO_DEADBAND_DPS = 1.6f;
constexpr float GYRO_TO_MOUSE_GAIN = 0.14f;
constexpr float SMOOTHING_ALPHA = 0.28f;
constexpr uint16_t CALIBRATION_SAMPLES = 400;
constexpr uint32_t SAMPLE_PERIOD_MS = 10;

constexpr float GESTURE_START_DPS = 115.0f;
constexpr float GESTURE_TOKEN_DPS = 50.0f;
constexpr uint32_t GESTURE_MAX_MS = 850;
constexpr uint32_t GESTURE_END_SILENCE_MS = 170;
constexpr uint32_t GESTURE_COOLDOWN_MS = 700;
constexpr uint32_t POST_GESTURE_CURSOR_HOLD_MS = 130;
constexpr float GESTURE_MIN_MATCH_SCORE = 0.72f;
constexpr size_t GESTURE_MIN_TOKENS = 3;

constexpr const char *PREF_NAMESPACE = "gestures";
constexpr const char *PREF_KEY_DB = "db";

float gyroBiasX = 0.0f;
float gyroBiasY = 0.0f;
float filteredDpsX = 0.0f;
float filteredDpsY = 0.0f;

struct GestureTemplate
{
  String name;
  String action;
  String tokens;
};

std::vector<GestureTemplate> gestureDb;
String serialLine;

bool trainingPending = false;
String trainingName;
String trainingAction;

bool gestureCapturing = false;
bool captureForTraining = false;
String captureTokens;
uint32_t gestureStartMs = 0;
uint32_t gestureLastActiveMs = 0;
uint32_t lastGestureFinishMs = 0;

float min3(float a, float b, float c)
{
  return min(a, min(b, c));
}

String normalizeName(String name)
{
  name.trim();
  name.toLowerCase();
  return name;
}

String normalizeAction(String action)
{
  action.trim();
  action.toLowerCase();
  return action;
}

int findGestureIndexByName(const String &name)
{
  for (size_t i = 0; i < gestureDb.size(); ++i)
  {
    if (gestureDb[i].name == name)
    {
      return static_cast<int>(i);
    }
  }
  return -1;
}

void saveGestureDb()
{
  String blob;
  for (size_t i = 0; i < gestureDb.size(); ++i)
  {
    if (i > 0)
    {
      blob += '\n';
    }
    blob += gestureDb[i].name;
    blob += '|';
    blob += gestureDb[i].action;
    blob += '|';
    blob += gestureDb[i].tokens;
  }
  preferences.putString(PREF_KEY_DB, blob);
}

void loadGestureDb()
{
  gestureDb.clear();
  String blob = preferences.getString(PREF_KEY_DB, "");
  if (blob.isEmpty())
  {
    return;
  }

  int start = 0;
  while (start < blob.length())
  {
    int end = blob.indexOf('\n', start);
    if (end < 0)
    {
      end = blob.length();
    }

    String line = blob.substring(start, end);
    line.trim();
    if (!line.isEmpty())
    {
      int p1 = line.indexOf('|');
      int p2 = p1 >= 0 ? line.indexOf('|', p1 + 1) : -1;
      if (p1 > 0 && p2 > p1 + 1 && p2 < line.length() - 1)
      {
        GestureTemplate item;
        item.name = normalizeName(line.substring(0, p1));
        item.action = normalizeAction(line.substring(p1 + 1, p2));
        item.tokens = line.substring(p2 + 1);
        if (!item.name.isEmpty() && !item.action.isEmpty() && !item.tokens.isEmpty())
        {
          gestureDb.push_back(item);
        }
      }
    }

    start = end + 1;
  }
}

void listGestures()
{
  if (gestureDb.empty())
  {
    Serial.println("No gestures defined.");
    return;
  }

  Serial.println("Saved gestures:");
  for (size_t i = 0; i < gestureDb.size(); ++i)
  {
    Serial.printf("  %u) name=%s action=%s tokens=%s\n",
                  static_cast<unsigned>(i + 1),
                  gestureDb[i].name.c_str(),
                  gestureDb[i].action.c_str(),
                  gestureDb[i].tokens.c_str());
  }
}

void printGestureHelp()
{
  Serial.println("Gesture commands:");
  Serial.println("  help");
  Serial.println("  list");
  Serial.println("  learn <name> <action>");
  Serial.println("  bind <name> <action>");
  Serial.println("  delete <name>");
  Serial.println("  clear");
  Serial.println("Actions now: left, right");
  Serial.println("Future-ready: key:<shortcut> (placeholder for keyboard actions)");
}

void upsertGesture(const String &name, const String &action, const String &tokens)
{
  int idx = findGestureIndexByName(name);
  if (idx >= 0)
  {
    gestureDb[static_cast<size_t>(idx)].action = action;
    gestureDb[static_cast<size_t>(idx)].tokens = tokens;
  }
  else
  {
    GestureTemplate item{name, action, tokens};
    gestureDb.push_back(item);
  }
  saveGestureDb();
}

bool bindGestureAction(const String &name, const String &action)
{
  int idx = findGestureIndexByName(name);
  if (idx < 0)
  {
    return false;
  }
  gestureDb[static_cast<size_t>(idx)].action = action;
  saveGestureDb();
  return true;
}

bool deleteGesture(const String &name)
{
  int idx = findGestureIndexByName(name);
  if (idx < 0)
  {
    return false;
  }
  gestureDb.erase(gestureDb.begin() + idx);
  saveGestureDb();
  return true;
}

char directionToken(float x, float y)
{
  const float ax = fabsf(x);
  const float ay = fabsf(y);
  if (ax < GESTURE_TOKEN_DPS && ay < GESTURE_TOKEN_DPS)
  {
    return '\0';
  }

  if (ax > ay * 1.35f)
  {
    return x >= 0.0f ? 'R' : 'L';
  }
  if (ay > ax * 1.35f)
  {
    return y >= 0.0f ? 'D' : 'U';
  }

  if (x >= 0.0f && y >= 0.0f)
  {
    return 'B';
  }
  if (x >= 0.0f && y < 0.0f)
  {
    return 'A';
  }
  if (x < 0.0f && y >= 0.0f)
  {
    return 'C';
  }
  return 'Z';
}

void appendToken(String &tokens, char token)
{
  if (token == '\0')
  {
    return;
  }
  if (tokens.isEmpty() || tokens[tokens.length() - 1] != token)
  {
    tokens += token;
  }
}

int levenshteinDistance(const String &a, const String &b)
{
  const int n = a.length();
  const int m = b.length();

  std::vector<int> prev(static_cast<size_t>(m + 1));
  std::vector<int> curr(static_cast<size_t>(m + 1));

  for (int j = 0; j <= m; ++j)
  {
    prev[static_cast<size_t>(j)] = j;
  }

  for (int i = 1; i <= n; ++i)
  {
    curr[0] = i;
    for (int j = 1; j <= m; ++j)
    {
      const int cost = (a[i - 1] == b[j - 1]) ? 0 : 1;
      curr[static_cast<size_t>(j)] = static_cast<int>(min3(
          static_cast<float>(curr[static_cast<size_t>(j - 1)] + 1),
          static_cast<float>(prev[static_cast<size_t>(j)] + 1),
          static_cast<float>(prev[static_cast<size_t>(j - 1)] + cost)));
    }
    prev.swap(curr);
  }

  return prev[static_cast<size_t>(m)];
}

bool findBestGestureMatch(const String &tokens, GestureTemplate &bestGesture, float &bestScore)
{
  bestScore = 0.0f;
  bool found = false;

  for (const GestureTemplate &item : gestureDb)
  {
    const int maxLen = max(tokens.length(), item.tokens.length());
    if (maxLen <= 0)
    {
      continue;
    }

    const int dist = levenshteinDistance(tokens, item.tokens);
    const float score = 1.0f - (static_cast<float>(dist) / static_cast<float>(maxLen));

    if (score > bestScore)
    {
      bestScore = score;
      bestGesture = item;
      found = true;
    }
  }

  return found && bestScore >= GESTURE_MIN_MATCH_SCORE;
}

void executeGestureAction(const String &action)
{
  const String normalized = normalizeAction(action);

  if (normalized == "left" || normalized == "left_click")
  {
    bleMouse.click(MOUSE_LEFT);
    Serial.println("Gesture action: LEFT click");
    return;
  }

  if (normalized == "right" || normalized == "right_click")
  {
    bleMouse.click(MOUSE_RIGHT);
    Serial.println("Gesture action: RIGHT click");
    return;
  }

  if (normalized.startsWith("key:"))
  {
    Serial.printf("Gesture matched key action placeholder: %s\n", normalized.c_str());
    return;
  }

  Serial.printf("Gesture matched unknown action: %s\n", normalized.c_str());
}

void startGestureCapture(bool trainingMode, uint32_t nowMs)
{
  gestureCapturing = true;
  captureForTraining = trainingMode;
  captureTokens = "";
  gestureStartMs = nowMs;
  gestureLastActiveMs = nowMs;

  if (trainingMode)
  {
    Serial.printf("Training capture started for '%s' -> action '%s'\n", trainingName.c_str(), trainingAction.c_str());
  }
  else
  {
    Serial.println("Gesture capture started.");
  }
}

void finalizeGestureCapture(uint32_t nowMs)
{
  gestureCapturing = false;
  lastGestureFinishMs = nowMs;

  if (captureTokens.length() < static_cast<int>(GESTURE_MIN_TOKENS))
  {
    Serial.println("Gesture ignored (too short).");
    if (captureForTraining && trainingPending)
    {
      Serial.println("Training still pending. Perform the gesture again.");
    }
    return;
  }

  if (captureForTraining && trainingPending)
  {
    upsertGesture(trainingName, trainingAction, captureTokens);
    Serial.printf("Gesture saved: name=%s action=%s tokens=%s\n",
                  trainingName.c_str(),
                  trainingAction.c_str(),
                  captureTokens.c_str());
    trainingPending = false;
    return;
  }

  GestureTemplate match;
  float score = 0.0f;
  if (findBestGestureMatch(captureTokens, match, score))
  {
    Serial.printf("Gesture matched '%s' (score=%.2f)\n", match.name.c_str(), score);
    executeGestureAction(match.action);
  }
  else
  {
    Serial.printf("No gesture match for tokens=%s\n", captureTokens.c_str());
  }
}

bool processGestureEngine(float xDps, float yDps, uint32_t nowMs)
{
  const float motionMag = sqrtf(xDps * xDps + yDps * yDps);

  if (!gestureCapturing)
  {
    if (nowMs - lastGestureFinishMs < GESTURE_COOLDOWN_MS)
    {
      return false;
    }

    if (motionMag >= GESTURE_START_DPS)
    {
      startGestureCapture(trainingPending, nowMs);
      appendToken(captureTokens, directionToken(xDps, yDps));
      return true;
    }

    return false;
  }

  if (motionMag >= GESTURE_TOKEN_DPS)
  {
    appendToken(captureTokens, directionToken(xDps, yDps));
    gestureLastActiveMs = nowMs;
  }

  const bool timeoutReached = (nowMs - gestureStartMs) >= GESTURE_MAX_MS;
  const bool settled = (nowMs - gestureLastActiveMs) >= GESTURE_END_SILENCE_MS &&
                       captureTokens.length() >= static_cast<int>(GESTURE_MIN_TOKENS);

  if (timeoutReached || settled)
  {
    finalizeGestureCapture(nowMs);
    return false;
  }

  return true;
}

void handleCommand(String cmdLine)
{
  cmdLine.trim();
  if (cmdLine.isEmpty())
  {
    return;
  }

  String lowerLine = cmdLine;
  lowerLine.toLowerCase();

  if (lowerLine == "help")
  {
    printGestureHelp();
    return;
  }

  if (lowerLine == "list")
  {
    listGestures();
    return;
  }

  if (lowerLine == "clear")
  {
    gestureDb.clear();
    saveGestureDb();
    Serial.println("All gestures cleared.");
    return;
  }

  if (lowerLine.startsWith("learn "))
  {
    String rest = cmdLine.substring(6);
    rest.trim();
    int split = rest.indexOf(' ');
    if (split < 1 || split >= rest.length() - 1)
    {
      Serial.println("Usage: learn <name> <action>");
      return;
    }

    trainingName = normalizeName(rest.substring(0, split));
    trainingAction = normalizeAction(rest.substring(split + 1));
    if (trainingName.isEmpty() || trainingAction.isEmpty())
    {
      Serial.println("Invalid name/action.");
      return;
    }

    trainingPending = true;
    Serial.printf("Learning armed for '%s' -> '%s'. Move now to record.\n",
                  trainingName.c_str(), trainingAction.c_str());
    return;
  }

  if (lowerLine.startsWith("bind "))
  {
    String rest = cmdLine.substring(5);
    rest.trim();
    int split = rest.indexOf(' ');
    if (split < 1 || split >= rest.length() - 1)
    {
      Serial.println("Usage: bind <name> <action>");
      return;
    }

    String name = normalizeName(rest.substring(0, split));
    String action = normalizeAction(rest.substring(split + 1));
    if (bindGestureAction(name, action))
    {
      Serial.printf("Updated gesture '%s' -> '%s'\n", name.c_str(), action.c_str());
    }
    else
    {
      Serial.printf("Gesture '%s' not found.\n", name.c_str());
    }
    return;
  }

  if (lowerLine.startsWith("delete "))
  {
    String name = normalizeName(cmdLine.substring(7));
    if (name.isEmpty())
    {
      Serial.println("Usage: delete <name>");
      return;
    }

    if (deleteGesture(name))
    {
      Serial.printf("Deleted gesture '%s'\n", name.c_str());
    }
    else
    {
      Serial.printf("Gesture '%s' not found.\n", name.c_str());
    }
    return;
  }

  Serial.println("Unknown command. Type 'help'.");
}

void handleSerialInput()
{
  while (Serial.available() > 0)
  {
    char ch = static_cast<char>(Serial.read());
    if (ch == '\r')
    {
      continue;
    }
    if (ch == '\n')
    {
      handleCommand(serialLine);
      serialLine = "";
      continue;
    }
    if (serialLine.length() < 160)
    {
      serialLine += ch;
    }
  }
}

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

  preferences.begin(PREF_NAMESPACE, false);
  loadGestureDb();

  bleMouse.begin();
  Serial.println("Pair to device: ESP32 MPU Mouse");
  Serial.println("Type 'help' in Serial Monitor for gesture commands.");

  if (gestureDb.empty())
  {
    Serial.println("No gestures saved. Example:");
    Serial.println("  learn lclick left");
    Serial.println("  learn rclick right");
  }
  else
  {
    listGestures();
  }
}

void loop()
{
  static uint32_t lastSampleMs = 0;
  static uint32_t lastStatusMs = 0;
  static uint32_t lastAdvertiseKickMs = 0;
  static bool wasConnected = false;
  const uint32_t nowMs = millis();
  const bool connected = bleMouse.isConnected();

  handleSerialInput();

  if (connected && !wasConnected)
  {
    Serial.println("BLE connected.");
  }

  if (!connected && wasConnected)
  {
    BLEDevice::startAdvertising();
    filteredDpsX = 0.0f;
    filteredDpsY = 0.0f;
    lastAdvertiseKickMs = nowMs;
    Serial.println("BLE disconnected. Re-advertising for any host...");
  }

  wasConnected = connected;

  if (nowMs - lastSampleMs < SAMPLE_PERIOD_MS)
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

  sensors_event_t a, g, temp;
  mpu.getEvent(&a, &g, &temp);

  float gyroXDps = (g.gyro.y - gyroBiasY) * RAD_TO_DEG_F;
  float gyroYDps = (g.gyro.x - gyroBiasX) * RAD_TO_DEG_F;

  gyroXDps = applyDeadband(gyroXDps, GYRO_DEADBAND_DPS);
  gyroYDps = applyDeadband(gyroYDps, GYRO_DEADBAND_DPS);

  filteredDpsX = (1.0f - SMOOTHING_ALPHA) * filteredDpsX + SMOOTHING_ALPHA * gyroXDps;
  filteredDpsY = (1.0f - SMOOTHING_ALPHA) * filteredDpsY + SMOOTHING_ALPHA * gyroYDps;

  const bool gestureInProgress = processGestureEngine(filteredDpsX, filteredDpsY, nowMs);
  const bool holdCursor = (nowMs - lastGestureFinishMs) < POST_GESTURE_CURSOR_HOLD_MS;
  if (gestureInProgress || holdCursor)
  {
    return;
  }

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