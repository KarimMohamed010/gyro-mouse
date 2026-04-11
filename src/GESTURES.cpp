
// #include <Arduino.h>
// #include <Adafruit_MPU6050.h>
// #include <Adafruit_Sensor.h>
// #include <Wire.h>
// #include <math.h>

// Adafruit_MPU6050 mpu;

// #define MAX_LEN 100
// #define TRAIN_SAMPLES 30

// // ---------------- STORAGE ----------------
// float RefrenceGesture[MAX_LEN][2];
// int RefrenceLen = 0;

// float testGesture[MAX_LEN][2];
// int testLen = 0;

// //------------DIRECTIONS----------------
// float refSumX = 0, refSumY = 0;
// float testSumX = 0, testSumY = 0;

// // ---------------- CALIBRATION ----------------
// float gyroBiasX = 0;
// float gyroBiasY = 0;

// constexpr float RAD_TO_DEG_F = 57.2958f;

// // ---------------- THRESHOLDS ----------------
// constexpr float MOTION_START_THRESHOLD = 1.5f;
// constexpr float MOTION_STOP_THRESHOLD = 1.0f;

// constexpr float RANGE_MARGIN = 2.0f;

// // ---------------- DTW ----------------
// float dtw(float a[][2], int lenA, float b[][2], int lenB)
// {
//   static float dp[MAX_LEN][MAX_LEN];

//   for (int i = 0; i < lenA; i++)
//     for (int j = 0; j < lenB; j++)
//       dp[i][j] = 1e9;

//   dp[0][0] = hypot(a[0][0] - b[0][0], a[0][1] - b[0][1]);

//   for (int i = 0; i < lenA; i++)
//   {
//     for (int j = 0; j < lenB; j++)
//     {
//       float cost = hypot(a[i][0] - b[j][0], a[i][1] - b[j][1]);

//       if (i > 0)
//         dp[i][j] = min(dp[i][j], dp[i - 1][j] + cost);
//       if (j > 0)
//         dp[i][j] = min(dp[i][j], dp[i][j - 1] + cost);
//       if (i > 0 && j > 0)
//         dp[i][j] = min(dp[i][j], dp[i - 1][j - 1] + cost);
//     }
//   }

//   return dp[lenA - 1][lenB - 1];
// }

// // ---------------- CALIBRATION ----------------
// void calibrate()
// {
//   float sx = 0, sy = 0;

//   Serial.println("Calibrating...");

//   for (int i = 0; i < 300; i++)
//   {
//     sensors_event_t a, g, t;
//     mpu.getEvent(&a, &g, &t);

//     sx += g.gyro.x;
//     sy += g.gyro.y;

//     delay(5);
//   }

//   gyroBiasX = sx / 300;
//   gyroBiasY = sy / 300;

//   Serial.println("Calibration done.");
// }

// // ---------------- RECORD GESTURE ----------------
// int recordGesture(float buffer[][2])
// {
//   int len = 0;
//   bool recording = false;
//   int still = 0;

//   while (true)
//   {
//     sensors_event_t a, g, t;
//     mpu.getEvent(&a, &g, &t);

//     float gx = (g.gyro.x - gyroBiasX) * RAD_TO_DEG_F;
//     float gy = (g.gyro.y - gyroBiasY) * RAD_TO_DEG_F;

//     float motion = sqrt(gx * gx + gy * gy);

//     if (!recording && motion > MOTION_START_THRESHOLD)
//     {
//       recording = true;
//       len = 0;
//       still = 0;
//     }

//     if (recording)
//     {
//       if (len < MAX_LEN)
//       {
//         buffer[len][0] = gx;
//         buffer[len][1] = gy;
//         len++;
//         if (motion < MOTION_STOP_THRESHOLD)
//           still++;
//         else
//           still = 0;

//         if (still > 20)
//           break;
//       }
//       else
//       {
//         break; // IMPORTANT FIX (prevents crash)
//       }
//     }

//     delay(10);
//   }

//   return len;
// }

// // ---------------- RANGE ----------------
// void computeRange(float arr[][2], int len,
//                   float &minX, float &maxX,
//                   float &minY, float &maxY)
// {
//   minX = 1e9;
//   maxX = -1e9;
//   minY = 1e9;
//   maxY = -1e9;

//   for (int i = 0; i < len; i++)
//   {
//     minX = min(minX, arr[i][0]);
//     maxX = max(maxX, arr[i][0]);
//     minY = min(minY, arr[i][1]);
//     maxY = max(maxY, arr[i][1]);
//   }
// }

// // ---------------- GLOBAL ACC ----------------
// float Acc[TRAIN_SAMPLES];
// float threshold = 0;

// // ---------------- TRAINING ----------------
// void trainThreshold()
// {
//   Serial.println("\n=== TRAINING MODE ===");

//   for (int i = 0; i < TRAIN_SAMPLES; i++)
//   {
//     Serial.print("Sample ");
//     Serial.println(i + 1);

//     testLen = recordGesture(testGesture);

//     float score = dtw(RefrenceGesture, RefrenceLen,
//                       testGesture, testLen);

//     score = score / max(RefrenceLen, testLen);

//     Acc[i] = score;

//     Serial.print("Score: ");
//     Serial.println(score);
//     // -------- DIRECTION DETECTION --------
//     String xdirection = "UNKNOWN";
//     String ydirection = "UNKNOWN";

//     if (abs(testSumX) > abs(testSumY))
//     {
//       if (testSumX > 0)
//         xdirection = "RIGHT";
//       else
//         xdirection = "LEFT";
//     }
//     else
//     {
//       if (testSumY > 0)
//         ydirection = "UP";
//       else
//         ydirection = "DOWN";
//     }

//     Serial.printf("Direction:x-> %s, y-> %s\n", xdirection.c_str(), ydirection.c_str());
//     delay(1500);
//   }

//   // ---------------- SORT ----------------
//   for (int i = 0; i < TRAIN_SAMPLES - 1; i++)
//   {
//     for (int j = i + 1; j < TRAIN_SAMPLES; j++)
//     {
//       if (Acc[j] < Acc[i])
//       {
//         float t = Acc[i];
//         Acc[i] = Acc[j];
//         Acc[j] = t;
//       }
//     }
//   }

//   // ---------------- THRESHOLD (90 percentile) ----------------
//   threshold = Acc[int(0.9 * TRAIN_SAMPLES)];

//   Serial.print("\nFINAL THRESHOLD = ");
//   Serial.println(threshold);
// }

// // ---------------- SETUP ----------------
// void setup()
// {
//   Serial.begin(115200);
//   delay(2000);

//   if (!mpu.begin())
//   {
//     Serial.println("MPU not found!");
//     while (1)
//       ;
//   }

//   calibrate();

//   Serial.println("Record REFERENCE gesture...");
//   RefrenceLen = recordGesture(RefrenceGesture);

//   refSumX = 0;
//   refSumY = 0;

//   for (int i = 0; i < RefrenceLen; i++)
//   {
//     refSumX += RefrenceGesture[i][0];
//     refSumY += RefrenceGesture[i][1];
//   }

//   Serial.println("Reference recorded");
//   delay(1000);
//   trainThreshold();
// }

// // ---------------- LOOP ----------------
// void loop()
// {
//   Serial.println("\nWaiting for gesture...");

//   testLen = recordGesture(testGesture);
//   testSumX = 0;
//   testSumY = 0;

//   for (int i = 0; i < testLen; i++)
//   {
//     testSumX += testGesture[i][0];
//     testSumY += testGesture[i][1];
//   }

//   float score = dtw(RefrenceGesture, RefrenceLen,
//                     testGesture, testLen);

//   score = score / max(RefrenceLen, testLen);

//   Serial.print("Score = ");
//   Serial.println(score);

//   // -------- DIRECTION DETECTION --------
//   String direction = "UNKNOWN";

//   if (abs(testSumX) > abs(testSumY))
//   {
//     if (testSumX > 0)
//       direction = "RIGHT";
//     else
//       direction = "LEFT";
//   }
//   else
//   {
//     if (testSumY > 0)
//       direction = "UP";
//     else
//       direction = "DOWN";
//   }

//   Serial.print("Direction: ");
//   Serial.println(direction);
//   // -------- direction check --------
//   float refDirX = (refSumX >= 0) ? 1 : -1;
//   float refDirY = (refSumY >= 0) ? 1 : -1;

//   float testDirX = (testSumX >= 0) ? 1 : -1;
//   float testDirY = (testSumY >= 0) ? 1 : -1;

//   // compare direction
//   bool directionMatch =
//       (refDirX == testDirX) &&
//       (refDirY == testDirY);

//   // final decision
//   if (score < threshold) // && directionMatch)
//     Serial.println("VALID GESTURE");
//   else
//   {
//     if (!directionMatch)
//       Serial.println("wrong direction\n");
//     Serial.println("INVALID GESTURE ");
//   }
//   delay(2000);
// }