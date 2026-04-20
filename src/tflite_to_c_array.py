#!/usr/bin/env python3
"""Convert a .tflite file into C array files for Arduino/TFLM."""

from __future__ import annotations

import argparse
from pathlib import Path


def emit_header(symbol: str) -> str:
    return (
        "#pragma once\n\n"
        "#include <stdint.h>\n\n"
        f"extern const unsigned char {symbol}[];\n"
        f"extern const int {symbol}_len;\n"
    )


def emit_source(symbol: str, data: bytes, header_name: str) -> str:
    lines = [f'#include "{header_name}"', "", f"const unsigned char {symbol}[] = {{"]

    for i in range(0, len(data), 12):
        chunk = data[i : i + 12]
        hexes = ", ".join(f"0x{b:02x}" for b in chunk)
        lines.append(f"    {hexes},")

    lines.extend([
        "};",
        "",
        f"const int {symbol}_len = sizeof({symbol});",
        "",
    ])
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="Convert .tflite to C array files.")
    parser.add_argument("--input", required=True, help="Input .tflite model path")
    parser.add_argument("--out-dir", default=".", help="Output directory")
    parser.add_argument("--symbol", default="g_gesture_model_data", help="C symbol name")
    parser.add_argument("--header", default="model_data.h", help="Header file name")
    parser.add_argument("--source", default="model_data.cpp", help="Source file name")
    args = parser.parse_args()

    in_path = Path(args.input)
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    data = in_path.read_bytes()
    header_text = emit_header(args.symbol)
    source_text = emit_source(args.symbol, data, args.header)

    (out_dir / args.header).write_text(header_text, encoding="utf-8")
    (out_dir / args.source).write_text(source_text, encoding="utf-8")

    print(f"Wrote {(out_dir / args.header).resolve()}")
    print(f"Wrote {(out_dir / args.source).resolve()}")
    print(f"Bytes: {len(data)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
