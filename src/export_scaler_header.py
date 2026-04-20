#!/usr/bin/env python3
"""Export sklearn StandardScaler stats to an Arduino C header."""

from __future__ import annotations

import argparse
from pathlib import Path

import joblib


def fmt_array(name: str, values: list[float]) -> str:
    lines = [f"constexpr float {name}[9] = {{"]
    lines.append("    " + ", ".join(f"{v:.8f}f" for v in values[0:3]) + ",")
    lines.append("    " + ", ".join(f"{v:.8f}f" for v in values[3:6]) + ",")
    lines.append("    " + ", ".join(f"{v:.8f}f" for v in values[6:9]) + ",")
    lines.append("};")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="Export scaler mean/std to feature_norm.h")
    parser.add_argument("--scaler", required=True, help="Path to scaler.joblib")
    parser.add_argument("--out", default="feature_norm.h", help="Output header path")
    args = parser.parse_args()

    scaler = joblib.load(args.scaler)
    mean = [float(v) for v in scaler.mean_.tolist()]
    std = [float(v) for v in scaler.scale_.tolist()]

    if len(mean) != 9 or len(std) != 9:
        raise ValueError("Scaler must contain exactly 9 features.")

    header = [
        "#pragma once",
        "",
        "// Auto-generated from sklearn StandardScaler.",
        fmt_array("kFeatureMean", mean),
        "",
        fmt_array("kFeatureStd", std),
        "",
    ]

    out_path = Path(args.out)
    out_path.write_text("\n".join(header), encoding="utf-8")
    print(f"Wrote {out_path.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
