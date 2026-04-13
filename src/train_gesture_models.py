#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np

from gesture_training.config import KNOWN_LABELS, PipelineConfig
from gesture_training.data import load_and_window_dataset
from gesture_training.evaluate import (
    evaluate_model,
    export_tflite,
    save_confusion_matrix,
    save_confusion_matrix_plot,
)
from gesture_training.models import build_cnn1d_model, build_lstm_model, default_callbacks
from gesture_training.preprocessing import (
    fit_label_encoder,
    normalize_sequences,
    save_preprocessors,
    stratified_split,
)



def parse_args():
    parser = argparse.ArgumentParser(description="Train MPU6050 gesture models (LSTM + 1D CNN).")
    parser.add_argument("--data-dir", required=True, help="Folder containing labeled CSV files.")
    parser.add_argument("--out-dir", default="training_artifacts", help="Output directory.")
    parser.add_argument("--window-size", type=int, default=100)
    parser.add_argument("--overlap", type=float, default=0.5)
    parser.add_argument("--epochs", type=int, default=40)
    parser.add_argument("--batch-size", type=int, default=64)
    parser.add_argument("--test-size", type=float, default=0.2)
    parser.add_argument("--val-size", type=float, default=0.2)
    parser.add_argument("--seed", type=int, default=42)
    return parser.parse_args()



def print_confusion_matrix(cm: np.ndarray, class_names: list[str]) -> None:
    print("Confusion matrix (rows=true, cols=pred):")
    header = " " * 12 + " ".join(f"{name:>10}" for name in class_names)
    print(header)
    for i, row in enumerate(cm):
        values = " ".join(f"{int(v):10d}" for v in row)
        print(f"{class_names[i]:>12} {values}")



def main() -> int:
    args = parse_args()

    cfg = PipelineConfig(
        window_size=args.window_size,
        overlap=args.overlap,
        random_state=args.seed,
        test_size=args.test_size,
        val_size=args.val_size,
        batch_size=args.batch_size,
        epochs=args.epochs,
    )

    data_dir = Path(args.data_dir)
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    dataset = load_and_window_dataset(data_dir, window_size=cfg.window_size, stride=cfg.stride)
    x, y_str = dataset.x, dataset.y

    print(f"Loaded windowed dataset: X={x.shape}, y={y_str.shape}")

    label_encoder = fit_label_encoder(y_str, known_labels=KNOWN_LABELS)
    y = label_encoder.transform(y_str)
    class_names = label_encoder.classes_.tolist()

    if len(np.unique(y)) < 2:
        raise ValueError("Need at least two gesture classes to train classifiers.")

    x_trainval, x_test, y_trainval, y_test = stratified_split(
        x, y, test_size=cfg.test_size, random_state=cfg.random_state
    )
    x_train, x_val, y_train, y_val = stratified_split(
        x_trainval, y_trainval, test_size=cfg.val_size, random_state=cfg.random_state
    )

    scaler, normalized = normalize_sequences(x_train, [x_val, x_test])
    x_train_n, x_val_n, x_test_n = normalized

    save_preprocessors(out_dir / "preprocessing", scaler, label_encoder)

    with (out_dir / "dataset_summary.json").open("w", encoding="utf-8") as f:
        json.dump(
            {
                "window_size": cfg.window_size,
                "stride": cfg.stride,
                "x_train": list(x_train_n.shape),
                "x_val": list(x_val_n.shape),
                "x_test": list(x_test_n.shape),
                "classes": class_names,
            },
            f,
            indent=2,
        )

    input_shape = (x_train_n.shape[1], x_train_n.shape[2])
    num_classes = len(class_names)

    models = {
        "lstm": build_lstm_model(input_shape=input_shape, num_classes=num_classes),
        "cnn1d": build_cnn1d_model(input_shape=input_shape, num_classes=num_classes),
    }

    metrics_summary = {}

    for model_name, model in models.items():
        print(f"\n=== Training {model_name.upper()} ===")
        history = model.fit(
            x_train_n,
            y_train,
            validation_data=(x_val_n, y_val),
            epochs=cfg.epochs,
            batch_size=cfg.batch_size,
            callbacks=default_callbacks(),
            verbose=2,
        )

        acc, cm, report = evaluate_model(model, x_test_n, y_test, class_names)
        print(f"Test accuracy ({model_name}): {acc:.4f}")
        print(report)
        print_confusion_matrix(cm, class_names)

        model_dir = out_dir / model_name
        model_dir.mkdir(parents=True, exist_ok=True)

        model.save(model_dir / f"{model_name}.keras")
        export_tflite(model, model_dir / f"{model_name}.tflite")

        save_confusion_matrix(cm, class_names, model_dir / "confusion_matrix.csv")
        plotted = save_confusion_matrix_plot(cm, class_names, model_dir / "confusion_matrix.png")

        history_path = model_dir / "history.json"
        with history_path.open("w", encoding="utf-8") as f:
            json.dump(history.history, f, indent=2)

        metrics_summary[model_name] = {
            "accuracy": float(acc),
            "confusion_matrix_csv": str((model_dir / "confusion_matrix.csv").resolve()),
            "confusion_matrix_png": str((model_dir / "confusion_matrix.png").resolve()) if plotted else None,
            "keras_model": str((model_dir / f"{model_name}.keras").resolve()),
            "tflite_model": str((model_dir / f"{model_name}.tflite").resolve()),
        }

    with (out_dir / "metrics_summary.json").open("w", encoding="utf-8") as f:
        json.dump(metrics_summary, f, indent=2)

    print("\nTraining complete.")
    print(f"Artifacts saved to: {out_dir.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
