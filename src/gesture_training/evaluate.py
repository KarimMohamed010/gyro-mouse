from __future__ import annotations

import csv
from pathlib import Path

import numpy as np
from sklearn.metrics import accuracy_score, classification_report, confusion_matrix


def _requires_select_tf_ops(model) -> bool:
    recurrent_layer_types = {"LSTM", "GRU", "SimpleRNN", "RNN", "Bidirectional"}
    return any(layer.__class__.__name__ in recurrent_layer_types for layer in model.layers)


def _enable_select_tf_ops(converter, tf) -> None:
    converter.target_spec.supported_ops = [
        tf.lite.OpsSet.TFLITE_BUILTINS,
        tf.lite.OpsSet.SELECT_TF_OPS,
    ]
    converter._experimental_lower_tensor_list_ops = False


def evaluate_model(model, x_test: np.ndarray, y_test: np.ndarray, class_names: list[str]):
    y_prob = model.predict(x_test, verbose=0)
    y_pred = np.argmax(y_prob, axis=1)

    acc = accuracy_score(y_test, y_pred)
    cm = confusion_matrix(y_test, y_pred, labels=list(range(len(class_names))))
    report = classification_report(
        y_test,
        y_pred,
        labels=range(len(class_names)),
        target_names=class_names,
        digits=4,
        zero_division=0,
    )
    return acc, cm, report



def save_confusion_matrix(cm: np.ndarray, class_names: list[str], out_csv: Path) -> None:
    out_csv.parent.mkdir(parents=True, exist_ok=True)
    with out_csv.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["true\\pred", *class_names])
        for idx, row in enumerate(cm):
            writer.writerow([class_names[idx], *row.tolist()])



def save_confusion_matrix_plot(cm: np.ndarray, class_names: list[str], out_png: Path) -> bool:
    try:
        import matplotlib.pyplot as plt
    except Exception:
        return False

    out_png.parent.mkdir(parents=True, exist_ok=True)

    fig, ax = plt.subplots(figsize=(6, 5))
    im = ax.imshow(cm, cmap="Blues")
    fig.colorbar(im, ax=ax)

    ax.set_xticks(np.arange(len(class_names)))
    ax.set_yticks(np.arange(len(class_names)))
    ax.set_xticklabels(class_names, rotation=45, ha="right")
    ax.set_yticklabels(class_names)
    ax.set_xlabel("Predicted")
    ax.set_ylabel("True")
    ax.set_title("Confusion Matrix")

    for i in range(cm.shape[0]):
        for j in range(cm.shape[1]):
            ax.text(j, i, int(cm[i, j]), ha="center", va="center", color="black")

    fig.tight_layout()
    fig.savefig(out_png, dpi=150)
    plt.close(fig)
    return True



def export_tflite(model, out_file: Path) -> None:
    out_file.parent.mkdir(parents=True, exist_ok=True)
    tf = __import__("tensorflow")
    converter = tf.lite.TFLiteConverter.from_keras_model(model)
    if _requires_select_tf_ops(model):
        _enable_select_tf_ops(converter, tf)

    try:
        tflite_data = converter.convert()
    except Exception:
        # Fallback for other conversion edge cases.
        converter = tf.lite.TFLiteConverter.from_keras_model(model)
        _enable_select_tf_ops(converter, tf)
        tflite_data = converter.convert()
    out_file.write_bytes(tflite_data)


def export_tflite_int8(model, representative_data: np.ndarray, out_file: Path) -> None:
    out_file.parent.mkdir(parents=True, exist_ok=True)
    tf = __import__("tensorflow")

    def representative_dataset_gen():
        max_items = min(300, representative_data.shape[0])
        for i in range(max_items):
            sample = representative_data[i : i + 1].astype(np.float32)
            yield [sample]

    if _requires_select_tf_ops(model):
        converter = tf.lite.TFLiteConverter.from_keras_model(model)
        _enable_select_tf_ops(converter, tf)
        tflite_data = converter.convert()
        out_file.write_bytes(tflite_data)
        return

    converter = tf.lite.TFLiteConverter.from_keras_model(model)
    converter.optimizations = [tf.lite.Optimize.DEFAULT]
    converter.representative_dataset = representative_dataset_gen
    converter.target_spec.supported_ops = [tf.lite.OpsSet.TFLITE_BUILTINS_INT8]
    converter.inference_input_type = tf.int8
    converter.inference_output_type = tf.int8

    try:
        tflite_data = converter.convert()
    except Exception:
        # Fallback when full INT8 lowering is unsupported.
        converter = tf.lite.TFLiteConverter.from_keras_model(model)
        _enable_select_tf_ops(converter, tf)
        tflite_data = converter.convert()

    out_file.write_bytes(tflite_data)
