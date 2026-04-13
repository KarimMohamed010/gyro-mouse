# MPU6050 Gesture Training Pipeline (TensorFlow/Keras)

This pipeline trains **two sequence models** on MPU6050 recordings:

- LSTM
- 1D CNN

It expects CSV rows with:

`timestamp,ax,ay,az,gx,gy,gz,yaw,pitch,roll,label`

## Features implemented

- Loads multiple CSV files from a folder recursively
- Segments into windows of 100 timesteps with 50% overlap (configurable)
- Normalizes features with `StandardScaler`
- Encodes labels (`swipe`, `shake`, `circle`, `wave`, `idle`)
- Trains LSTM and 1D CNN with TensorFlow/Keras
- Prints test accuracy + confusion matrix
- Saves trained `.keras` model files
- Exports `.tflite` files
- Saves confusion matrices and training history

## Install

```bash
pip install -r requirements-training.txt
```

## Train

```bash
python train_gesture_models.py --data-dir recordings --out-dir training_artifacts
```

## Optional arguments

```bash
python train_gesture_models.py \
  --data-dir recordings \
  --out-dir training_artifacts \
  --window-size 100 \
  --overlap 0.5 \
  --epochs 40 \
  --batch-size 64 \
  --test-size 0.2 \
  --val-size 0.2 \
  --seed 42
```

## Output structure

- `training_artifacts/preprocessing/scaler.joblib`
- `training_artifacts/preprocessing/label_classes.json`
- `training_artifacts/lstm/lstm.keras`
- `training_artifacts/lstm/lstm.tflite`
- `training_artifacts/lstm/confusion_matrix.csv`
- `training_artifacts/lstm/confusion_matrix.png` (if matplotlib available)
- `training_artifacts/cnn1d/cnn1d.keras`
- `training_artifacts/cnn1d/cnn1d.tflite`
- `training_artifacts/cnn1d/confusion_matrix.csv`
- `training_artifacts/cnn1d/confusion_matrix.png` (if matplotlib available)
- `training_artifacts/metrics_summary.json`

## Notes

- If a class is missing from your dataset, training still works as long as at least two classes exist.
- Unknown labels outside `swipe/shake/circle/wave/idle` raise an explicit error.
