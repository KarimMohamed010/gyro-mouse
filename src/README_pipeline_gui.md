# Gesture Pipeline GUI

This GUI replaces repeated terminal commands and lets you run the full workflow:

1. Record labeled gesture data
2. Train LSTM + CNN models
3. Deploy model and scaler artifacts to firmware files

## Launch

```bash
python pipeline_gui.py
```

## Tabs

## 1) Record

- Select serial `Port` and `Baud`
- Click `Connect Stream`
- Choose output folder
- Choose active label (`swipe`, `shake`, `circle`, `wave`, `idle`)
- Click `Start Recording` / `Stop Recording`

Each recording is saved as a separate CSV with:

`timestamp,ax,ay,az,gx,gy,gz,yaw,pitch,roll,label`

## 2) Train

- Choose `Data Folder` (recordings)
- Choose `Output Folder` (training_artifacts)
- Set `Epochs` and `Batch Size`
- Click `Run Training`

This runs `train_gesture_models.py` and logs output in the GUI.

## 3) Deploy

- Pick model file (usually `cnn1d_int8.tflite`)
- Set C symbol (default `g_gesture_model_data`)
- Choose output directory (typically `src`)
- Pick scaler `.joblib`
- Set `feature_norm.h` output path

Buttons:

- `Convert Model -> C Array`
- `Export Scaler Header`
- `Run Both`

## Notes

- Keep ESP32 streaming at ~50 Hz with 9-feature CSV lines.
- `ml_gestures` outputs will appear as:

`ml_gestures,<label>,<score>`
