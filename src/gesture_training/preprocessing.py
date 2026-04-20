from __future__ import annotations

import json
from pathlib import Path

import joblib
import numpy as np
from sklearn.model_selection import train_test_split
from sklearn.preprocessing import LabelEncoder, StandardScaler



def stratified_split(x: np.ndarray, y: np.ndarray, test_size: float, random_state: int):
    # Fallback to non-stratified split for very small class counts.
    try:
        return train_test_split(
            x,
            y,
            test_size=test_size,
            random_state=random_state,
            stratify=y,
        )
    except ValueError:
        return train_test_split(
            x,
            y,
            test_size=test_size,
            random_state=random_state,
            stratify=None,
        )



def normalize_sequences(x_train: np.ndarray, x_other: list[np.ndarray]):
    scaler = StandardScaler()

    n_steps = x_train.shape[1]
    n_features = x_train.shape[2]

    x_train_flat = x_train.reshape(-1, n_features)
    scaler.fit(x_train_flat)

    def _transform(x: np.ndarray) -> np.ndarray:
        flat = x.reshape(-1, n_features)
        scaled = scaler.transform(flat)
        return scaled.reshape(-1, n_steps, n_features).astype(np.float32)

    transformed = [_transform(x_train)]
    transformed.extend(_transform(x) for x in x_other)
    return scaler, transformed



def fit_label_encoder(y: np.ndarray, known_labels: list[str]) -> LabelEncoder:
    encoder = LabelEncoder()
    # Fit on known labels for stable class index ordering.
    encoder.fit(known_labels)

    unknown = sorted(set(y) - set(known_labels))
    if unknown:
        raise ValueError(f"Unknown labels found in dataset: {unknown}")

    return encoder



def save_preprocessors(out_dir: Path, scaler: StandardScaler, label_encoder: LabelEncoder) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)

    joblib.dump(scaler, out_dir / "scaler.joblib")
    with (out_dir / "label_classes.json").open("w", encoding="utf-8") as f:
        json.dump(label_encoder.classes_.tolist(), f, indent=2)
