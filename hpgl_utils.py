"""
Pure-text HPGL helpers.

These functions never touch the plotter. They turn the HPGL stream produced by
vpype into the list of short commands the serial worker sends one at a time,
each followed by an ``OE;`` handshake.
"""
from __future__ import annotations

import re

# Number of coordinate pairs allowed in a single PD/PU command. The Roland
# DXY-1200 input buffer is small, so long polylines are chunked.
DEFAULT_MAX_PAIRS = 50

_SP_RE = re.compile(r"^SP(\d*)$")
_PEN_MOVE_RE = re.compile(r"^(P[UD])([-\d,\s]*)$")


def split_hpgl_into_commands(hpgl_str: str) -> list[str]:
    """Split an HPGL string on semicolons, dropping whitespace and empty parts."""
    return [part.strip() for part in hpgl_str.split(";") if part.strip()]


def split_command_into_pairs(command_str: str, max_pairs: int = DEFAULT_MAX_PAIRS) -> list[str]:
    """
    Split a single PD/PU command carrying many coordinate pairs into several
    commands with at most ``max_pairs`` pairs each. Returns commands terminated
    with ``;``.
    """
    i = 0
    while i < len(command_str) and command_str[i].isalpha():
        i += 1

    prefix = command_str[:i]
    coords = command_str[i:].strip()

    if not coords:
        return [prefix + ";"]

    numbers = coords.split(",")
    if len(numbers) % 2 != 0:
        raise ValueError(f"Odd number of coordinate values in {command_str!r}")

    pairs = list(zip(numbers[0::2], numbers[1::2]))
    return [
        prefix + ",".join(f"{x},{y}" for x, y in pairs[start : start + max_pairs]) + ";"
        for start in range(0, len(pairs), max_pairs)
    ]


def prepare_job_commands(
    hpgl_str: str,
    pen_speeds: dict[int, int] | None = None,
    default_speed: int | None = None,
    max_pairs: int = DEFAULT_MAX_PAIRS,
) -> list[str]:
    """
    Turn a raw HPGL stream into the command list for a job.

    - PD/PU commands are chunked to ``max_pairs`` pairs.
    - Every ``SPn`` (n > 0) is followed by ``VS<speed>`` for that pen, taken
      from ``pen_speeds`` or ``default_speed``.
    - Any ``VS`` already in the stream is dropped (speeds are ours to set).
    - The job always ends with the pen lifted and returned to the carousel
      (``PU;`` then ``SP0;``) so a finished plot never leaves a pen out.
    """
    pen_speeds = pen_speeds or {}
    result: list[str] = []

    for cmd in split_hpgl_into_commands(hpgl_str):
        if cmd.startswith(("PD", "PU")):
            result.extend(split_command_into_pairs(cmd, max_pairs=max_pairs))
            continue

        if cmd.startswith("VS"):
            continue

        m = _SP_RE.match(cmd)
        if m:
            pen = int(m.group(1)) if m.group(1) else 0
            result.append(f"SP{pen};")
            speed = pen_speeds.get(pen, default_speed)
            if pen > 0 and speed:
                result.append(f"VS{int(speed)};")
            continue

        result.append(cmd + ";")

    return ensure_parked(result)


def ensure_parked(commands: list[str]) -> list[str]:
    """
    Make sure a job ends with the pen lifted and returned to the carousel:
    the last two commands become ``PU...;`` then ``SP0;``. A trailing ``IN;``
    (vpype emits one) is dropped since re-initialising after parking does
    nothing useful.
    """
    out = list(commands)
    while out and out[-1] == "IN;":
        out.pop()
    if len(out) >= 2 and out[-1] == "SP0;" and out[-2].startswith("PU"):
        return out
    if out and out[-1] == "SP0;":
        out.pop()
    if not out or not out[-1].startswith("PU"):
        out.append("PU;")
    out.append("SP0;")
    return out


def pens_used(commands: list[str]) -> list[int]:
    """Return the distinct pen numbers (excluding 0) selected in a command list, in order."""
    seen: list[int] = []
    for cmd in commands:
        m = _SP_RE.match(cmd.rstrip(";"))
        if m and m.group(1):
            pen = int(m.group(1))
            if pen > 0 and pen not in seen:
                seen.append(pen)
    return seen


def jog_commands(direction: str, distance: int, pen_down: bool = False) -> str:
    """
    Build a relative move. Wrapped in ``PR;`` / ``PA;`` so the plotter is always
    left in absolute mode, regardless of what came before.
    """
    distance = int(distance)
    dx, dy = {
        "up": (0, distance),
        "down": (0, -distance),
        "left": (-distance, 0),
        "right": (distance, 0),
    }[direction]
    verb = "PD" if pen_down else "PU"
    return f"PR;{verb}{dx},{dy};PA;"


def pen_test_commands(pens: list[int], length_units: int = 800, spacing_units: int = 400,
                      origin: tuple[int, int] = (500, 500), pen_speeds: dict[int, int] | None = None) -> list[str]:
    """
    Draw one short horizontal stroke per pen so carousel positions can be
    checked. Rows are stacked upwards from ``origin`` in plotter units.
    """
    pen_speeds = pen_speeds or {}
    x0, y0 = origin
    cmds = ["IN;", "PA;"]
    for row, pen in enumerate(pens):
        y = y0 + row * spacing_units
        cmds.append(f"SP{pen};")
        if pen_speeds.get(pen):
            cmds.append(f"VS{int(pen_speeds[pen])};")
        cmds.append(f"PU{x0},{y};")
        cmds.append(f"PD{x0 + length_units},{y};")
        cmds.append("PU;")
    cmds.append("SP0;")
    return cmds


def hpgl_bounds(commands: list[str]) -> tuple[int, int, int, int] | None:
    """
    Bounding box (x0, y0, x1, y1) of every PU/PD coordinate in a command list,
    in plotter units. Coordinates sent while in relative mode (``PR;`` until
    ``PA;``) are ignored. Returns None if no coordinates are present.
    """
    xs: list[int] = []
    ys: list[int] = []
    relative = False
    for cmd in commands:
        body = cmd.strip().rstrip(";")
        verb = body[:2].upper()
        if verb == "PR":
            relative = True
            continue
        if verb in ("PA", "IN"):
            relative = False
            continue
        if relative:
            continue
        m = _PEN_MOVE_RE.match(body)
        if not m or not m.group(2).strip():
            continue
        nums = [n.strip() for n in m.group(2).split(",") if n.strip()]
        try:
            values = [int(n) for n in nums]
        except ValueError:
            continue
        xs.extend(values[0::2])
        ys.extend(values[1::2])
    if not xs or not ys:
        return None
    return min(xs), min(ys), max(xs), max(ys)


def outline_commands(rects: list[tuple[int, int, int, int]], passes: int = 1) -> list[str]:
    """
    Trace rectangles with the pen holder empty and raised, so the user can see
    where a plot will land. ``rects`` are (x0, y0, x1, y1) in plotter units.
    Each corner is a separate command, so the carriage pauses briefly at each.
    """
    cmds = ["IN;", "PA;", "SP0;"]
    for x0, y0, x1, y1 in rects:
        x0, x1 = sorted((int(x0), int(x1)))
        y0, y1 = sorted((int(y0), int(y1)))
        corners = [(x0, y0), (x1, y0), (x1, y1), (x0, y1), (x0, y0)]
        for _ in range(max(1, passes)):
            cmds.extend(f"PU{x},{y};" for x, y in corners)
    cmds.append("PU0,0;")
    cmds.append("SP0;")
    return cmds
