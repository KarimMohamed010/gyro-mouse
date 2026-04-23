#include <Arduino.h>
#include "I2Cdev.h"
#include "MPU6050_6Axis_MotionApps20.h"
#include <Wire.h>
#include <BleMouse.h>
#include <BLEDevice.h>
#include <ArduinoJson.h>
#include <Preferences.h>

#include "model_data.h"

#if __has_include(<TensorFlowLite.h>)
#include <TensorFlowLite.h>
#include "tensorflow/lite/micro/micro_error_reporter.h"
#include "tensorflow/lite/micro/micro_interpreter.h"
#include "tensorflow/lite/micro/micro_mutable_op_resolver.h"
#include "tensorflow/lite/schema/schema_generated.h"
#include "tensorflow/lite/version.h"
#define HAS_TFLM 1
#else
#define HAS_TFLM 0
#endif

// ── Hardware ──────────────────────────────────────────────────────────────────
constexpr uint8_t INTERRUPT_PIN = 2;

// ── Fixed tuning (not user-configurable) ─────────────────────────────────────
constexpr uint16_t BLE_POST_CONNECT_DELAY_MS = 1200;
constexpr uint16_t BLE_REPORT_INTERVAL_MS = 16;
constexpr uint16_t GESTURE_COOLDOWN_MS = 800;
constexpr uint16_t SHAKE_WINDOW_MS = 800;
constexpr uint16_t DOUBLE_TILT_WINDOW_MS = 500;
constexpr uint8_t SHAKE_REVERSALS_REQUIRED = 4;
constexpr uint8_t CIRCLE_FRAMES_REQUIRED = 12;

// ── Streaming and inference settings ─────────────────────────────────────────
constexpr uint16_t SERIAL_STREAM_INTERVAL_MS = 20; // ~50 Hz
constexpr uint16_t INFERENCE_STRIDE_SAMPLES = 10;  // run every 10 new samples

// ── Axis indices ──────────────────────────────────────────────────────────────
// The DMP gives us yaw/pitch/roll. We expose them as A0/A1/A2 to the config.
// 0 = yaw, 1 = pitch, 2 = roll
constexpr uint8_t AXIS_YAW = 0;
constexpr uint8_t AXIS_PITCH = 1;
constexpr uint8_t AXIS_ROLL = 2;

constexpr int kFeatureCount = 9; // ax,ay,az,gx,gy,gz,yaw,pitch,roll
constexpr int kWindowSize = 100;

#if __has_include("feature_norm.h")
#include "feature_norm.h"
#define HAS_FEATURE_NORM_HEADER 1
#else
#define HAS_FEATURE_NORM_HEADER 0
#endif

#if !HAS_FEATURE_NORM_HEADER
constexpr float kFeatureMean[kFeatureCount] = {
    0.f,
    0.f,
    0.f,
    0.f,
    0.f,
    0.f,
    0.f,
    0.f,
    0.f,
};
constexpr float kFeatureStd[kFeatureCount] = {
    1.f,
    1.f,
    1.f,
    1.f,
    1.f,
    1.f,
    1.f,
    1.f,
    1.f,
};
#endif

constexpr const char *kMlGestureLabels[] = {"swipe", "shake", "circle", "wave", "idle"};
constexpr int kMlLabelCount = sizeof(kMlGestureLabels) / sizeof(kMlGestureLabels[0]);

// ── User config (runtime, overwritten by GUI via Serial) ─────────────────────
struct Config
{
  // Axis mapping: which DMP axis drives each function
  uint8_t cursorXAxis = AXIS_YAW;  // default: yaw -> cursor X
  uint8_t cursorYAxis = AXIS_ROLL; // default: roll -> cursor Y
  uint8_t clickAxis = AXIS_PITCH;  // default: pitch -> left/right click

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

  // Tilt threshold placeholder (reserved for future tilt actions)
  float tiltThreshDeg = 30.0f;

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

Preferences prefs;

// ── Helpers ───────────────────────────────────────────────────────────────────
inline int8_t signOf(float v)
{
  return (v > 0.f) ? 1 : (v < 0.f) ? -1
                                   : 0;
}

inline float applyDeadband(float v, float db)
{
  return fabsf(v) < db ? 0.f : v;
}

// Raw DMP angles array indexed by AXIS_* constants
float axes[3] = {}; // [yaw, pitch, roll] in degrees, updated each frame

inline float getAxis(uint8_t idx) { return axes[idx]; }

// Persistent config helpers (Preferences)
void saveConfig()
{
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
}

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

  // Flick
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

  // Shake
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

  // Double-tilt
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

  // Circle
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

// ── Serial config parser ─────────────────────────────────────────────────────
void handleSerial()
{
  if (!Serial.available())
    return;

  String line = Serial.readStringUntil('\n');
  line.trim();
  if (line.length() == 0)
    return;

  if (line == "{\"ping\":1}")
  {
    Serial.println("{\"pong\":1}");
    return;
  }

  JsonDocument doc;
  if (deserializeJson(doc, line) != DeserializationError::Ok)
    return;
  // Support explicit save command: {"save":1}
  if (doc["save"].is<int>() && doc["save"].as<int>() == 1)
  {
    saveConfig();
    Serial.println("{\"ack\":\"saved\"}");
    return;
  }
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
  if (c["tiltThreshDeg"].is<float>())
    cfg.tiltThreshDeg = c["tiltThreshDeg"].as<float>();
  else if (c["clickThreshDeg"].is<float>())
    cfg.tiltThreshDeg = c["clickThreshDeg"].as<float>();
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

  // Auto-save after applying new config so changes persist immediately
  saveConfig();
  Serial.println("{\"ack\":\"cfg\"}");
}

// ── MPU/DMP objects ─────────────────────────────────────────────────────────
MPU6050 mpu;
BleMouse bleMouse("ESP32 MPU Mouse", "Espressif", 100);

bool dmpReady = false;
uint8_t devStatus = 0;
uint8_t fifoBuffer[64] = {0};

Quaternion q;
VectorFloat gravity;
VectorInt16 aa;
VectorInt16 gg;
float ypr[3] = {0.f, 0.f, 0.f};

namespace ml_gestures
{
  float featureBuffer[kWindowSize][kFeatureCount] = {};
  int ringWriteIndex = 0;
  int bufferedSamples = 0;
  uint16_t samplesSinceInference = 0;

#if HAS_TFLM
  // ── TensorFlow Lite Micro state ─────────────────────────────────────────────
  constexpr size_t kTensorArenaSize = 120 * 1024;
  alignas(16) static uint8_t tensorArena[kTensorArenaSize];

  tflite::MicroErrorReporter microErrorReporter;
  const tflite::Model *model = nullptr;
  tflite::MicroInterpreter *interpreter = nullptr;
  TfLiteTensor *inputTensor = nullptr;
  TfLiteTensor *outputTensor = nullptr;

  bool tflmReady = false;
  bool tflmWarned = false;

  inline float normalizeFeature(int idx, float value)
  {
    const float s = kFeatureStd[idx];
    return (s > 1e-6f) ? ((value - kFeatureMean[idx]) / s) : (value - kFeatureMean[idx]);
  }

  bool setupTFLM()
  {
    model = tflite::GetModel(g_gesture_model_data);
    if (!model)
    {
      Serial.println("TFLM model pointer is null.");
      return false;
    }
    if (model->version() != TFLITE_SCHEMA_VERSION)
    {
      Serial.println("TFLM model schema version mismatch.");
      return false;
    }

    static tflite::MicroMutableOpResolver<18> opResolver;
    opResolver.AddReshape();
    opResolver.AddConv2D();
    opResolver.AddDepthwiseConv2D();
    opResolver.AddFullyConnected();
    opResolver.AddSoftmax();
    opResolver.AddMean();
    opResolver.AddMul();
    opResolver.AddAdd();
    opResolver.AddQuantize();
    opResolver.AddDequantize();
    opResolver.AddRelu();
    opResolver.AddPad();
    opResolver.AddPack();
    opResolver.AddStridedSlice();
    opResolver.AddExpandDims();
    opResolver.AddSqueeze();
    opResolver.AddMaxPool2D();
    opResolver.AddLogistic();

    static tflite::MicroInterpreter staticInterpreter(model, opResolver, tensorArena, kTensorArenaSize, &microErrorReporter);
    interpreter = &staticInterpreter;

    if (interpreter->AllocateTensors() != kTfLiteOk)
    {
      Serial.println("AllocateTensors failed.");
      return false;
    }

    inputTensor = interpreter->input(0);
    outputTensor = interpreter->output(0);

    if (!inputTensor || !outputTensor)
    {
      Serial.println("Input/output tensor missing.");
      return false;
    }

    Serial.print("TFLM ready. Arena bytes: ");
    Serial.println(kTensorArenaSize);
    return true;
  }

  void fillModelInputFromRing()
  {
    const int inputElements = inputTensor->bytes / ((inputTensor->type == kTfLiteFloat32) ? sizeof(float) : sizeof(int8_t));
    const int expectedElements = kWindowSize * kFeatureCount;
    if (inputElements != expectedElements)
    {
      if (!tflmWarned)
      {
        Serial.print("Input shape mismatch. expected=");
        Serial.print(expectedElements);
        Serial.print(" got=");
        Serial.println(inputElements);
        tflmWarned = true;
      }
      return;
    }

    int flatIdx = 0;
    const int oldestIndex = (ringWriteIndex + kWindowSize - bufferedSamples) % kWindowSize;

    for (int i = 0; i < kWindowSize; i++)
    {
      const int srcIdx = (oldestIndex + i) % kWindowSize;
      for (int f = 0; f < kFeatureCount; f++)
      {
        const float normalized = normalizeFeature(f, featureBuffer[srcIdx][f]);
        if (inputTensor->type == kTfLiteFloat32)
        {
          inputTensor->data.f[flatIdx] = normalized;
        }
        else if (inputTensor->type == kTfLiteInt8)
        {
          const float invScale = 1.0f / inputTensor->params.scale;
          int32_t qv = static_cast<int32_t>(roundf(normalized * invScale) + inputTensor->params.zero_point);
          if (qv > 127)
            qv = 127;
          if (qv < -128)
            qv = -128;
          inputTensor->data.int8[flatIdx] = static_cast<int8_t>(qv);
        }
        flatIdx++;
      }
    }
  }

  void runInferenceAndPrint()
  {
    if (!tflmReady)
      return;
    if (bufferedSamples < kWindowSize)
      return;

    fillModelInputFromRing();

    if (interpreter->Invoke() != kTfLiteOk)
    {
      Serial.println("Inference failed.");
      return;
    }

    const int outCount = outputTensor->dims->data[outputTensor->dims->size - 1];
    if (outCount <= 0)
      return;

    int bestIdx = 0;
    float bestScore = -1.0f;
    for (int i = 0; i < outCount; i++)
    {
      float score = 0.f;
      if (outputTensor->type == kTfLiteFloat32)
      {
        score = outputTensor->data.f[i];
      }
      else if (outputTensor->type == kTfLiteInt8)
      {
        score = (static_cast<int32_t>(outputTensor->data.int8[i]) - outputTensor->params.zero_point) * outputTensor->params.scale;
      }
      else if (outputTensor->type == kTfLiteUInt8)
      {
        score = (static_cast<int32_t>(outputTensor->data.uint8[i]) - outputTensor->params.zero_point) * outputTensor->params.scale;
      }

      if (score > bestScore)
      {
        bestScore = score;
        bestIdx = i;
      }
    }

    Serial.print("ml_gestures,");
    if (bestIdx >= 0 && bestIdx < kMlLabelCount)
    {
      Serial.print(kMlGestureLabels[bestIdx]);
    }
    else
    {
      Serial.print("class_");
      Serial.print(bestIdx);
    }
    Serial.print(",");
    Serial.println(bestScore, 4);
  }
#endif

  void pushFeatureSample(const float sample[kFeatureCount])
  {
    for (int i = 0; i < kFeatureCount; i++)
      featureBuffer[ringWriteIndex][i] = sample[i];

    ringWriteIndex = (ringWriteIndex + 1) % kWindowSize;
    if (bufferedSamples < kWindowSize)
      bufferedSamples++;
    samplesSinceInference++;
  }

  void setup()
  {
#if HAS_TFLM
    tflmReady = setupTFLM();
#else
    Serial.println("ml_gestures: TensorFlow Lite Micro headers not found. Disabled.");
#endif
  }

  void onSample(const float sample[kFeatureCount])
  {
    pushFeatureSample(sample);
#if HAS_TFLM
    if (samplesSinceInference >= INFERENCE_STRIDE_SAMPLES)
    {
      runInferenceAndPrint();
      samplesSinceInference = 0;
    }
#endif
  }
} // namespace ml_gestures

volatile bool mpuInterrupt = false;
void IRAM_ATTR dmpDataReady() { mpuInterrupt = true; }

void setup()
{
  Wire.begin();
  Wire.setClock(400000);
  Serial.begin(115200);
  while (!Serial)
    ;

  // Load persisted config (if any)
  loadConfig();

  pinMode(INTERRUPT_PIN, INPUT);

  Serial.println("Initializing MPU6050...");
  mpu.initialize();
  if (!mpu.testConnection())
  {
    Serial.println("MPU6050 connection failed.");
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
    Serial.println("Calibrating MPU6050... keep sensor still");
    mpu.CalibrateAccel(6);
    mpu.CalibrateGyro(6);
    mpu.setDMPEnabled(true);
    attachInterrupt(digitalPinToInterrupt(INTERRUPT_PIN), dmpDataReady, RISING);
    dmpReady = true;
    Serial.println("DMP ready.");
  }
  else
  {
    Serial.print("DMP init failed code=");
    Serial.println(devStatus);
    while (true)
      ;
  }

  bleMouse.begin();
  Serial.println("BLE advertising as: ESP32 MPU Mouse");
  ml_gestures::setup();

  Serial.println("Output format:");
  Serial.println("  RAW: ax,ay,az,gx,gy,gz,yaw,pitch,roll");
  Serial.println("  PRED: ml_gestures,<label>,<score>");
}

void loop()
{
  if (!dmpReady)
    return;

  handleSerial();

  static uint32_t lastStatusMs = 0;
  static uint32_t lastAdvertiseKickMs = 0;
  static uint32_t connectedSinceMs = 0;
  static uint32_t lastReportMs = 0;
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
  }

  const bool mouseReportsEnabled = connected && ((nowMs - connectedSinceMs) >= BLE_POST_CONNECT_DELAY_MS);

  static uint32_t lastSampleMs = 0;
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

  const float yaw = ypr[0] * RAD_TO_DEG;
  const float pitch = ypr[1] * RAD_TO_DEG;
  const float roll = ypr[2] * RAD_TO_DEG;

  axes[AXIS_YAW] = yaw;
  axes[AXIS_PITCH] = pitch;
  axes[AXIS_ROLL] = roll;

  float sample[kFeatureCount] = {
      static_cast<float>(aa.x),
      static_cast<float>(aa.y),
      static_cast<float>(aa.z),
      static_cast<float>(gg.x),
      static_cast<float>(gg.y),
      static_cast<float>(gg.z),
      yaw,
      pitch,
      roll,
  };

  // Stream raw features for recording/debugging.
  Serial.print(sample[0], 3);
  for (int i = 1; i < kFeatureCount; i++)
  {
    Serial.print(',');
    Serial.print(sample[i], 3);
  }
  Serial.println();

  ml_gestures::onSample(sample);

  // Keep original cursor/tilt/manual-gesture pipeline intact.
  const float rawX = applyDeadband(getAxis(cfg.cursorXAxis), cfg.deadzoneX);
  const float rawY = applyDeadband(getAxis(cfg.cursorYAxis), cfg.deadzoneY);
  const float rawTilt = applyDeadband(getAxis(cfg.clickAxis), cfg.deadzoneClick);

  const float cursorX = rawX * cfg.gainX * (cfg.invertX ? -1.f : 1.f);
  const float cursorY = rawY * cfg.gainY * (cfg.invertY ? -1.f : 1.f);
  const float tiltV = rawTilt * (cfg.invertClick ? -1.f : 1.f);

  detectGestures(nowMs);

  // Placeholder only: keep threshold checks but intentionally do nothing for now.
  const bool tiltPastNegative = mouseReportsEnabled && (tiltV < -cfg.tiltThreshDeg);
  const bool tiltPastPositive = mouseReportsEnabled && (tiltV > cfg.tiltThreshDeg);
  (void)tiltPastNegative;
  (void)tiltPastPositive;

  const int moveX = constrain(static_cast<int>(roundf(cursorX)), -127, 127);
  const int moveY = constrain(static_cast<int>(roundf(-cursorY)), -127, 127);

  if (mouseReportsEnabled && (moveX != 0 || moveY != 0) && (nowMs - lastReportMs >= BLE_REPORT_INTERVAL_MS))
  {
    bleMouse.move(static_cast<int8_t>(moveX), static_cast<int8_t>(moveY));
    lastReportMs = nowMs;
  }
}