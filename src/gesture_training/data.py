from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

import numpy as np
import pandas as pd

from .config import FEATURE_COLUMNS


@dataclass
class WindowedDataset:
    x: np.ndarray
    y: np.ndarray



def list_csv_files(dataset_dir: Path) -> list[Path]:
    files = sorted(dataset_dir.rglob("*.csv"))
    if not files:
        raise FileNotFoundError(f"No CSV files found in {dataset_dir}")
    return files



def _majority_label(labels: Iterable[str]) -> str:
    # pandas mode handles ties deterministically by sorting.
    series = pd.Series(labels)
    modes = series.mode()
    if modes.empty:
        return "idle"
    return str(modes.iloc[0])



def _window_file(df: pd.DataFrame, window_size: int, stride: int) -> WindowedDataset:
    features = df[FEATURE_COLUMNS].to_numpy(dtype=np.float32)
    labels = df["label"].astype(str).to_numpy()

    n = len(df)
    if n < window_size:
        return WindowedDataset(
            x=np.empty((0, window_size, len(FEATURE_COLUMNS)), dtype=np.float32),
            y=np.empty((0,), dtype=object),
        )

    x_windows = []
    y_windows = []
    for start in range(0, n - window_size + 1, stride):
        end = start + window_size
        x_windows.append(features[start:end])
        y_windows.append(_majority_label(labels[start:end]))

    return WindowedDataset(x=np.asarray(x_windows, dtype=np.float32), y=np.asarray(y_windows, dtype=object))



def load_and_window_dataset(dataset_dir: Path, window_size: int, stride: int) -> WindowedDataset:
    csv_files = list_csv_files(dataset_dir)

    xs = []
    ys = []
    required_columns = {"timestamp", *FEATURE_COLUMNS, "label"}

    for file_path in csv_files:
        df = pd.read_csv(file_path)
        missing = required_columns - set(df.columns)
        if missing:
            raise ValueError(f"{file_path} is missing columns: {sorted(missing)}")

        windowed = _window_file(df, window_size=window_size, stride=stride)
        if len(windowed.x) == 0:
            continue

        xs.append(windowed.x)
        ys.append(windowed.y)

    if not xs:
        raise ValueError("No valid windows were created. Check dataset length/window size.")

    return WindowedDataset(
        x=np.concatenate(xs, axis=0),
        y=np.concatenate(ys, axis=0),
    )
