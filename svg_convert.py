"""
SVG analysis and SVG -> HPGL conversion built on vpype's Python API.

Coordinate model
----------------
Everything is laid out in *bed* coordinates: the plotter's own frame, in mm,
X along the long edge, Y along the short edge, origin at the bottom-left.
The bed size comes from the ``x_range`` / ``y_range`` of the ``bed`` paper in
``printerconfig/roland_dxy1200.toml``.

A job is placed inside a *plot area*: a rectangle on the bed (the sheet or
card, or an area captured from the pen position). The drawing is optionally
rotated 90 degrees to match the area's orientation, either scaled to fit the
area with a margin or kept at its SVG size, cropped to the area, and then
translated to the area's position on the bed. The HPGL writer therefore
always receives a bed-sized document and never has to rotate or crop, which
is what used to cut the top off portrait pages.

The SVG is read with one layer per stroke colour. Colours are merged into one
layer per *pen*, each pen layer is optimised independently, and the document
is written once; vpype's HPGL writer maps layer id to pen number.
"""
from __future__ import annotations

import io
import math
import os
import re
from dataclasses import dataclass, field

import vpype as vp
from vpype_cli import execute

from hpgl_utils import prepare_job_commands, pens_used

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(HERE, "printerconfig", "roland_dxy1200.toml")
DEVICE = "roland_dxy1200"
BED_PAPER = "bed"

MM_PER_PX = 25.4 / 96.0
PX_PER_MM = 96.0 / 25.4
QUANTIZATION_PX = 0.1 * PX_PER_MM  # 0.1 mm

# Rough timing model for the DXY-1200 (VS is cm/s).
PEN_UP_SPEED_CM_S = 40.0
PEN_CHANGE_SECONDS = 4.0
DEFAULT_SPEED = 10

vp.config_manager.load_config_file(CONFIG_PATH)

_plotter_config = vp.config_manager.get_plotter_config(DEVICE)
_bed_config = _plotter_config.paper_config(BED_PAPER)
PLOTTER_UNIT_MM = round(float(_plotter_config.plotter_unit_length) * MM_PER_PX, 6)
BED_X_RANGE = tuple(_bed_config.x_range)
BED_Y_RANGE = tuple(_bed_config.y_range)
BED_W_MM = round((BED_X_RANGE[1] - BED_X_RANGE[0]) * PLOTTER_UNIT_MM, 3)
BED_H_MM = round((BED_Y_RANGE[1] - BED_Y_RANGE[0]) * PLOTTER_UNIT_MM, 3)

_XML_PROLOG_RE = re.compile(r"^\s*<\?xml[^>]*\?>")
_SVG_OPEN_RE = re.compile(r"<svg\b[^>]*>")


@dataclass(frozen=True)
class Rect:
    """A rectangle in bed coordinates (mm, origin bottom-left, Y up)."""
    x: float
    y: float
    w: float
    h: float

    @property
    def x2(self) -> float:
        return self.x + self.w

    @property
    def y2(self) -> float:
        return self.y + self.h

    @property
    def landscape(self) -> bool:
        return self.w >= self.h

    def clamped_to_bed(self) -> "Rect":
        """Intersect with the bed; an empty intersection becomes a 1 mm square at the origin."""
        x1 = min(max(self.x, 0.0), BED_W_MM)
        y1 = min(max(self.y, 0.0), BED_H_MM)
        x2 = min(max(self.x2, 0.0), BED_W_MM)
        y2 = min(max(self.y2, 0.0), BED_H_MM)
        if x2 - x1 < 1.0 or y2 - y1 < 1.0:
            return Rect(x1, y1, max(1.0, min(x2 - x1, 1.0)), max(1.0, min(y2 - y1, 1.0)))
        return Rect(x1, y1, x2 - x1, y2 - y1)

    def to_units(self) -> tuple[int, int, int, int]:
        """(x0, y0, x1, y1) in plotter units."""
        return (
            int(round(self.x / PLOTTER_UNIT_MM)),
            int(round(self.y / PLOTTER_UNIT_MM)),
            int(round(self.x2 / PLOTTER_UNIT_MM)),
            int(round(self.y2 / PLOTTER_UNIT_MM)),
        )

    @staticmethod
    def from_units(x0: int, y0: int, x1: int, y1: int) -> "Rect":
        return Rect(x0 * PLOTTER_UNIT_MM, y0 * PLOTTER_UNIT_MM,
                    (x1 - x0) * PLOTTER_UNIT_MM, (y1 - y0) * PLOTTER_UNIT_MM)


BED = Rect(0.0, 0.0, BED_W_MM, BED_H_MM)

# Paper presets: key -> (label, width mm, height mm) as loaded on the bed
# (long edge along X). Users can swap or edit the numbers freely.
PAPER_PRESETS: dict[str, tuple[str, float, float]] = {
    "a3": ("A3", 420.0, 297.0),
    "a4": ("A4", 297.0, 210.0),
    "a5": ("A5", 210.0, 148.0),
    "a6": ("A6", 148.0, 105.0),
    "card_7x5": ("7 x 5 in card", 177.8, 127.0),
    "card_6x4": ("6 x 4 in card", 152.4, 101.6),
    "square_150": ("150 mm square", 150.0, 150.0),
    "dl": ("DL envelope", 220.0, 110.0),
    "bed": ("Whole bed", round(BED_W_MM, 1), round(BED_H_MM, 1)),
}
DEFAULT_PRESET = "a4"

ROTATIONS = {"auto": "Auto (match area)", "none": "As drawn", "rotate": "Rotate 90°"}


def _float(value, default: float) -> float:
    try:
        f = float(value)
    except (TypeError, ValueError):
        return default
    if math.isnan(f) or math.isinf(f):
        return default
    return f


def area_from_form(form, prefix: str = "area_") -> Rect:
    """Read area_x/area_y/area_w/area_h (mm) from a form, defaulting to the default preset at the origin."""
    _, dw, dh = PAPER_PRESETS[DEFAULT_PRESET]
    return Rect(
        x=max(0.0, _float(form.get(prefix + "x"), 0.0)),
        y=max(0.0, _float(form.get(prefix + "y"), 0.0)),
        w=max(1.0, _float(form.get(prefix + "w"), dw)),
        h=max(1.0, _float(form.get(prefix + "h"), dh)),
    )


@dataclass
class ColorInfo:
    color: str          # "#rrggbb"
    lines: int
    length_mm: float


@dataclass
class PlotOptions:
    area: Rect = field(default_factory=lambda: Rect(0.0, 0.0, PAPER_PRESETS[DEFAULT_PRESET][1], PAPER_PRESETS[DEFAULT_PRESET][2]))
    fit: bool = True            # True: scale to fit the area with margin; False: keep SVG size, bottom-left aligned
    margin_mm: float = 10.0
    rotation: str = "auto"      # auto | none | rotate

    @classmethod
    def from_form(cls, form) -> "PlotOptions":
        rotation = form.get("rotation", "auto")
        if rotation not in ROTATIONS:
            rotation = "auto"
        return cls(
            area=area_from_form(form),
            fit=form.get("fit", "fit") != "keep",
            margin_mm=max(0.0, _float(form.get("margin_mm"), 10.0)),
            rotation=rotation,
        )


@dataclass
class PenStats:
    pen: int
    color: str
    speed: int
    lines: int
    down_mm: float
    up_mm: float
    seconds: float


@dataclass
class ConversionResult:
    commands: list[str]
    preview_svg: str
    area: Rect
    bounds: Rect | None                 # drawn extent on the bed, mm
    pens: list[PenStats] = field(default_factory=list)
    total_down_mm: float = 0.0
    total_up_mm: float = 0.0
    estimated_seconds: float = 0.0
    rotated: bool = False
    scale: float = 1.0                  # applied to the SVG geometry
    cropped_fraction: float = 0.0       # share of drawn length lost outside the area
    warnings: list[str] = field(default_factory=list)

    @property
    def pen_numbers(self) -> list[int]:
        return pens_used(self.commands)

    @property
    def bounds_units(self) -> tuple[int, int, int, int] | None:
        return self.bounds.to_units() if self.bounds else None


def _read(svg_path: str) -> vp.Document:
    return vp.read_svg_by_attributes(svg_path, ["stroke"], quantization=QUANTIZATION_PX)


def _layer_hex(layer: vp.LineCollection) -> str:
    if layer.property_exists(vp.METADATA_FIELD_COLOR):
        color = layer.property(vp.METADATA_FIELD_COLOR)
        try:
            return color.as_rgb_hex().lower()
        except Exception:
            pass
    return "#000000"


def _layers_by_color(doc: vp.Document) -> dict[str, vp.LineCollection]:
    """Merge vpype's per-stroke layers into one LineCollection per hex colour."""
    merged: dict[str, vp.LineCollection] = {}
    for layer in doc.layers.values():
        if layer.is_empty():
            continue
        key = _layer_hex(layer)
        if key in merged:
            merged[key].extend(layer)
        else:
            merged[key] = vp.LineCollection(layer)
    return merged


def analyze_svg(svg_path: str) -> list[ColorInfo]:
    """List the stroke colours found in an SVG, black first, then by drawn length."""
    doc = _read(svg_path)
    infos = [
        ColorInfo(color=c, lines=len(lc), length_mm=float(lc.length()) * MM_PER_PX)
        for c, lc in _layers_by_color(doc).items()
    ]
    infos.sort(key=lambda i: (i.color != "#000000", -i.length_mm))
    return infos


def _total_length(doc: vp.Document) -> float:
    return float(sum(layer.length() for layer in doc.layers.values()))


def _page_of(doc: vp.Document) -> tuple[float, float]:
    """The SVG page in px; falls back to the geometry bounds (moved to the origin)."""
    if doc.page_size and doc.page_size[0] > 0 and doc.page_size[1] > 0:
        return doc.page_size
    b = doc.bounds()
    if b is None:
        return (1.0, 1.0)
    doc.translate(-b[0], -b[1])
    return (max(1.0, b[2] - b[0]), max(1.0, b[3] - b[1]))


def _place_in_area(doc: vp.Document, options: PlotOptions, area: Rect) -> tuple[bool, float]:
    """
    Rotate / scale / position ``doc`` so that its coordinates are relative to
    the area's top-left corner (SVG convention, y down), in px.
    Returns (rotated, scale).
    """
    page_w, page_h = _page_of(doc)

    rotate = options.rotation == "rotate" or (
        options.rotation == "auto" and (page_w > page_h) != area.landscape and page_w != page_h
    )
    if rotate:
        # (x, y) -> (y, -x) then shift back into positive y: top of the drawing ends up on the left.
        doc.rotate(-math.pi / 2)
        doc.translate(0, page_w)
        page_w, page_h = page_h, page_w

    area_w, area_h = area.w * PX_PER_MM, area.h * PX_PER_MM
    scale = 1.0

    if options.fit:
        b = doc.bounds()
        if b is not None:
            min_x, min_y, max_x, max_y = b
            width = max(max_x - min_x, 1e-6)
            height = max(max_y - min_y, 1e-6)
            margin = min(options.margin_mm * PX_PER_MM, area_w / 2 - 0.5, area_h / 2 - 0.5)
            margin = max(margin, 0.0)
            scale = min((area_w - 2 * margin) / width, (area_h - 2 * margin) / height)
            doc.translate(-min_x, -min_y)
            doc.scale(scale)
            width *= scale
            height *= scale
            doc.translate((area_w - width) / 2, (area_h - height) / 2)
    else:
        # Keep SVG scale; align the SVG page's bottom-left with the area's bottom-left.
        doc.translate(0, area_h - page_h)

    doc.page_size = (area_w, area_h)
    return rotate, scale


def _preview_svg(doc: vp.Document, area: Rect, bounds: Rect | None) -> str:
    """Render the bed with the plot area and the drawing on it."""
    buf = io.StringIO()
    vp.write_svg(buf, doc, page_size=doc.page_size, color_mode="default", layer_label_format="Pen %d")
    svg = _XML_PROLOG_RE.sub("", buf.getvalue(), count=1).strip()

    bed_w, bed_h = BED_W_MM * PX_PER_MM, BED_H_MM * PX_PER_MM

    def rect_px(r: Rect) -> tuple[float, float, float, float]:
        return (r.x * PX_PER_MM, (BED_H_MM - r.y2) * PX_PER_MM, r.w * PX_PER_MM, r.h * PX_PER_MM)

    ax, ay, aw, ah = rect_px(area)
    tick = 6 * PX_PER_MM
    parts = [
        f'<g id="bed" fill="none">'
        f'<rect x="0" y="0" width="{bed_w:.2f}" height="{bed_h:.2f}" fill="#dfe3e8"/>'
        f'<rect x="{ax:.2f}" y="{ay:.2f}" width="{aw:.2f}" height="{ah:.2f}" fill="#ffffff" stroke="#9aa3ad" stroke-width="1.5"/>'
        f'<path d="M0,{bed_h:.2f} L{tick:.2f},{bed_h:.2f} M0,{bed_h:.2f} L0,{bed_h - tick:.2f}" stroke="#c2410c" stroke-width="3"/>'
    ]
    if bounds is not None:
        bx, by, bw, bh = rect_px(bounds)
        parts.append(
            f'<rect x="{bx:.2f}" y="{by:.2f}" width="{bw:.2f}" height="{bh:.2f}" '
            f'fill="none" stroke="#2563eb" stroke-width="1" stroke-dasharray="6 4" opacity="0.8"/>'
        )
    parts.append("</g>")
    return _SVG_OPEN_RE.sub(lambda m: m.group(0) + "".join(parts), svg, count=1)


def convert_svg(
    svg_path: str,
    color_to_pen: dict[str, int],
    pen_speeds: dict[int, int],
    pen_colors: dict[int, str] | None = None,
    options: PlotOptions | None = None,
    default_pen: int = 1,
) -> ConversionResult:
    """
    Convert an SVG to a job command list plus a preview and statistics.

    Args:
        svg_path: path to the uploaded SVG
        color_to_pen: hex colour (lowercase) -> pen slot
        pen_speeds: pen slot -> VS value
        pen_colors: pen slot -> display colour for the preview
        options: plot area / placement options
        default_pen: pen used for colours missing from ``color_to_pen``
    """
    options = options or PlotOptions()
    pen_colors = pen_colors or {}
    warnings: list[str] = []

    area = options.area.clamped_to_bed()
    if (abs(area.w - options.area.w) > 0.5 or abs(area.h - options.area.h) > 0.5
            or abs(area.x - options.area.x) > 0.5 or abs(area.y - options.area.y) > 0.5):
        warnings.append(
            f"Plot area extends past the bed; trimmed to {area.w:.1f} x {area.h:.1f} mm at ({area.x:.0f}, {area.y:.0f})."
        )

    src = _read(svg_path)
    doc = vp.Document(page_size=src.page_size)

    for color, lc in _layers_by_color(src).items():
        pen = int(color_to_pen.get(color.lower(), default_pen))
        if pen in doc.layers:
            doc.layers[pen].extend(lc)
        else:
            doc.add(lc, pen)

    for pen, layer in doc.layers.items():
        layer.set_property(vp.METADATA_FIELD_COLOR, vp.Color(pen_colors.get(pen, "#000000")))
        layer.set_property(vp.METADATA_FIELD_NAME, f"Pen {pen}")

    rotated, scale = _place_in_area(doc, options, area)

    # Crop to the area and measure what was lost.
    length_before = _total_length(doc)
    doc.crop(0.0, 0.0, area.w * PX_PER_MM, area.h * PX_PER_MM)
    length_after = _total_length(doc)
    cropped = 0.0 if length_before <= 0 else max(0.0, 1.0 - length_after / length_before)
    if cropped > 0.001:
        warnings.append(f"{cropped * 100:.0f}% of the drawing lies outside the plot area and will not be drawn.")
    if length_after <= 0:
        warnings.append("Nothing of the drawing falls inside the plot area.")

    doc = execute("linemerge linesimplify linesort", doc)

    # Move onto the bed (SVG coordinates: y down from the top edge of the bed).
    doc.translate(area.x * PX_PER_MM, (BED_H_MM - area.y2) * PX_PER_MM)
    doc.page_size = (BED_W_MM * PX_PER_MM, BED_H_MM * PX_PER_MM)

    b = doc.bounds()
    bounds = None
    if b is not None:
        bounds = Rect(float(b[0] * MM_PER_PX), float(BED_H_MM - b[3] * MM_PER_PX),
                      float((b[2] - b[0]) * MM_PER_PX), float((b[3] - b[1]) * MM_PER_PX))

    speeds = {pen: int(pen_speeds.get(pen, DEFAULT_SPEED) or DEFAULT_SPEED) for pen in doc.layers}

    hpgl = io.StringIO()
    vp.write_hpgl(
        hpgl, doc,
        page_size=BED_PAPER,
        landscape=BED_W_MM >= BED_H_MM,
        center=False,
        device=DEVICE,
        velocity=None,
        absolute=True,
        quiet=True,
    )
    commands = prepare_job_commands(hpgl.getvalue(), pen_speeds=speeds)

    stats: list[PenStats] = []
    total_down = total_up = total_seconds = 0.0
    for pen in sorted(doc.layers):
        layer = doc.layers[pen]
        if layer.is_empty():
            continue
        down_mm = float(layer.length()) * MM_PER_PX
        up_mm = float(layer.pen_up_length()[0]) * MM_PER_PX
        speed = speeds[pen]
        seconds = (down_mm / 10.0) / speed + (up_mm / 10.0) / PEN_UP_SPEED_CM_S + PEN_CHANGE_SECONDS
        stats.append(PenStats(
            pen=pen, color=pen_colors.get(pen, "#000000"), speed=speed,
            lines=len(layer), down_mm=down_mm, up_mm=up_mm, seconds=seconds,
        ))
        total_down += down_mm
        total_up += up_mm
        total_seconds += seconds

    return ConversionResult(
        commands=commands,
        preview_svg=_preview_svg(doc, area, bounds),
        area=area,
        bounds=bounds,
        pens=stats,
        total_down_mm=total_down,
        total_up_mm=total_up,
        estimated_seconds=total_seconds,
        rotated=rotated,
        scale=scale,
        cropped_fraction=cropped,
        warnings=warnings,
    )


def format_duration(seconds: float) -> str:
    seconds = int(round(seconds))
    if seconds < 60:
        return f"{seconds}s"
    minutes, s = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}m {s:02d}s"
    hours, m = divmod(minutes, 60)
    return f"{hours}h {m:02d}m"
