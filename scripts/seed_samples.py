#!/usr/bin/env python3
"""Regenerate the placeholder sample images + manifest.json (stdlib only)."""
from __future__ import annotations

import json
import struct
import zlib
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ROUND = "sample-hero-round-1"
ROUND_DIR = ROOT / "needs-review" / ROUND
THUMB_DIR = ROUND_DIR / "thumbs"


def png_rgb(w: int, h: int, rgb: tuple[int, int, int], caption: str = "") -> bytes:
    r, g, b = rgb
    raw = bytearray()
    for y in range(h):
        raw.append(0)
        for x in range(w):
            if x < 4 or y < 4 or x >= w - 4 or y >= h - 4:
                raw.extend((255, 255, 255))
            else:
                c = 24 if ((x // 16) + (y // 16)) % 2 == 0 else 0
                raw.extend((min(255, r + c), min(255, g + c), min(255, b + c)))
    for y in range(20, min(52, h - 4)):
        for x in range(20, w - 20):
            i = 1 + y * (w * 3 + 1) + x * 3
            raw[i : i + 3] = bytes((10, 10, 10))
    glyphs = {
        "S": ["01110", "10000", "01110", "00001", "01110"],
        "A": ["01110", "10001", "11111", "10001", "10001"],
        "M": ["10001", "11011", "10101", "10001", "10001"],
        "P": ["11110", "10001", "11110", "10000", "10000"],
        "L": ["10000", "10000", "10000", "10000", "11111"],
        "E": ["11111", "10000", "11110", "10000", "11111"],
        " ": ["00000", "00000", "00000", "00000", "00000"],
        "R": ["11110", "10001", "11110", "10100", "10010"],
        "D": ["11100", "10010", "10001", "10010", "11100"],
        "B": ["11110", "10001", "11110", "10001", "11110"],
        "U": ["10001", "10001", "10001", "10001", "01110"],
        "G": ["01110", "10000", "10111", "10001", "01110"],
        "N": ["10001", "11001", "10101", "10011", "10001"],
        "-": ["00000", "00000", "11111", "00000", "00000"],
        "1": ["00100", "01100", "00100", "00100", "01110"],
        "2": ["01110", "10001", "00110", "01000", "11111"],
        "3": ["11110", "00001", "01110", "00001", "11110"],
        "4": ["10010", "10010", "11111", "00010", "00010"],
    }

    def blit(text: str, ox: int, oy: int, scale: int = 3, color=(240, 240, 240)) -> None:
        x = ox
        for ch in text:
            g = glyphs.get(ch.upper(), glyphs[" "])
            for gy, row in enumerate(g):
                for gx, bit in enumerate(row):
                    if bit != "1":
                        continue
                    for dy in range(scale):
                        for dx in range(scale):
                            px, py = x + gx * scale + dx, oy + gy * scale + dy
                            if 0 <= px < w and 0 <= py < h:
                                i = 1 + py * (w * 3 + 1) + px * 3
                                raw[i : i + 3] = bytes(color)
            x += 6 * scale

    blit("SAMPLE", 28, 24, scale=max(2, min(4, w // 96)))
    if caption:
        blit(caption[:12], 28, 70 if h > 100 else 40, scale=max(2, min(3, w // 128)))

    def chunk(tag: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    ihdr = struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", zlib.compress(bytes(raw), 9)) + chunk(b"IEND", b"")


def main() -> None:
    ROUND_DIR.mkdir(parents=True, exist_ok=True)
    THUMB_DIR.mkdir(parents=True, exist_ok=True)
    samples = [
        ("A1", "red", (180, 40, 40), "RED"),
        ("A2", "blue", (40, 80, 200), "BLUE"),
        ("A3", "green", (40, 160, 70), "GREEN"),
        ("B1", "amber", (200, 140, 30), "AMBER"),
    ]
    now = datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")
    entries = []
    for cell, key, rgb, label in samples:
        fname = f"{cell}__sample-hero-{key}.png"
        tname = f"{cell}__sample-hero-{key}_thumb.png"
        full = ROUND_DIR / fname
        thumb = THUMB_DIR / tname
        full.write_bytes(png_rgb(384, 384, rgb, f"PLACEHOLDER-{label}"))
        thumb.write_bytes(png_rgb(128, 128, rgb, label))
        entries.append(
            {
                "id": f"{ROUND}-{cell}",
                "round": ROUND,
                "asset": "sample-hero",
                "status": "needs-review",
                "spark_path": str(full.relative_to(ROOT)),
                "thumb_path": str(thumb.relative_to(ROOT)),
                "source_path": str(full.relative_to(ROOT)),
                "source_job": "sample/seed",
                "prompt": f"PLACEHOLDER SAMPLE — {label}. Replace with your own generations.",
                "model": "none (placeholder)",
                "seed": 0,
                "size": "384x384",
                "cell": cell,
                "key": key,
                "label": f"Sample hero — {label} (placeholder)",
                "note": "",
                "added_at": now,
                "reviewed_at": None,
                "asset_class": "character",
                "act": None,
            }
        )
    (ROOT / "manifest.json").write_text(json.dumps(entries, indent=2) + "\n")
    print(f"Seeded {len(entries)} placeholders under {ROUND_DIR}")


if __name__ == "__main__":
    main()
