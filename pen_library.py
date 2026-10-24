"""
Persistent description of what is loaded in each carousel slot, plus a memory
of which pen each SVG colour was last assigned to.

Stored as JSON next to the app (``pens.json``), created on first save.
"""
from __future__ import annotations

import json
import os
import threading

PEN_COUNT = 8
MAX_SPEED = 42  # DXY-1200 VS range is 1-42 cm/s

DEFAULT_PENS = {
    1: {"name": "Black", "color": "#000000", "speed": 10},
    2: {"name": "Red", "color": "#d62828", "speed": 10},
    3: {"name": "Blue", "color": "#1d4ed8", "speed": 10},
    4: {"name": "Green", "color": "#15803d", "speed": 10},
    5: {"name": "Orange", "color": "#f77f00", "speed": 10},
    6: {"name": "Purple", "color": "#7c3aed", "speed": 10},
    7: {"name": "Brown", "color": "#92400e", "speed": 10},
    8: {"name": "Grey", "color": "#6b7280", "speed": 10},
}


def _hex_to_rgb(color: str) -> tuple[int, int, int]:
    color = color.lstrip("#")
    if len(color) == 3:
        color = "".join(ch * 2 for ch in color)
    try:
        return int(color[0:2], 16), int(color[2:4], 16), int(color[4:6], 16)
    except ValueError:
        return 0, 0, 0


def normalize_hex(color: str) -> str:
    r, g, b = _hex_to_rgb(color or "#000000")
    return f"#{r:02x}{g:02x}{b:02x}"


def clamp_speed(value) -> int:
    try:
        return max(1, min(MAX_SPEED, int(value)))
    except (TypeError, ValueError):
        return 10


class PenLibrary:
    def __init__(self, path: str):
        self.path = path
        self._lock = threading.Lock()
        self.pens: dict[int, dict] = {n: dict(p) for n, p in DEFAULT_PENS.items()}
        self.color_memory: dict[str, int] = {}
        self.load()

    # ---------------------------------------------------------------- persistence
    def load(self) -> None:
        if not os.path.exists(self.path):
            return
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            return
        for key, pen in (data.get("pens") or {}).items():
            try:
                slot = int(key)
            except ValueError:
                continue
            if 1 <= slot <= PEN_COUNT and isinstance(pen, dict):
                self.pens[slot] = {
                    "name": str(pen.get("name", f"Pen {slot}"))[:40],
                    "color": normalize_hex(pen.get("color", "#000000")),
                    "speed": clamp_speed(pen.get("speed", 10)),
                }
        for color, slot in (data.get("color_memory") or {}).items():
            try:
                slot = int(slot)
            except (TypeError, ValueError):
                continue
            if 1 <= slot <= PEN_COUNT:
                self.color_memory[normalize_hex(color)] = slot

    def save(self) -> None:
        with self._lock:
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(
                    {"pens": {str(n): p for n, p in self.pens.items()},
                     "color_memory": self.color_memory},
                    f, indent=2,
                )
            os.replace(tmp, self.path)

    # ---------------------------------------------------------------- accessors
    def all(self) -> list[dict]:
        return [{"slot": n, **self.pens[n]} for n in range(1, PEN_COUNT + 1)]

    def get(self, slot: int) -> dict:
        return self.pens.get(int(slot), DEFAULT_PENS[1])

    def speeds(self) -> dict[int, int]:
        return {n: p["speed"] for n, p in self.pens.items()}

    def colors(self) -> dict[int, str]:
        return {n: p["color"] for n, p in self.pens.items()}

    def update_from_form(self, form) -> None:
        for slot in range(1, PEN_COUNT + 1):
            name = form.get(f"name_{slot}")
            if name is None:
                continue
            self.pens[slot] = {
                "name": (name.strip() or f"Pen {slot}")[:40],
                "color": normalize_hex(form.get(f"color_{slot}", "#000000")),
                "speed": clamp_speed(form.get(f"speed_{slot}", 10)),
            }
        self.save()

    # ---------------------------------------------------------------- suggestions
    def suggest(self, color: str) -> int:
        """Pen slot for an SVG colour: remembered mapping first, else nearest colour."""
        color = normalize_hex(color)
        if color in self.color_memory:
            return self.color_memory[color]
        r, g, b = _hex_to_rgb(color)
        best, best_d = 1, float("inf")
        for slot, pen in self.pens.items():
            pr, pg, pb = _hex_to_rgb(pen["color"])
            d = (r - pr) ** 2 + (g - pg) ** 2 + (b - pb) ** 2
            if d < best_d:
                best, best_d = slot, d
        return best

    def remember(self, mapping: dict[str, int]) -> None:
        changed = False
        for color, slot in mapping.items():
            color = normalize_hex(color)
            if self.color_memory.get(color) != slot:
                self.color_memory[color] = int(slot)
                changed = True
        if changed:
            self.save()
