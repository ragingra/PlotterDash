import re

from svg_convert import (
    BED, BED_H_MM, BED_W_MM, PlotOptions, Rect, analyze_svg, area_from_form, convert_svg, format_duration,
)

A4 = Rect(0, 0, 297, 210)


def _coords(commands):
    for c in commands:
        if c.startswith(("PD", "PU")) and len(c) > 3:
            nums = [int(n) for n in c[2:-1].split(",")]
            yield from zip(nums[0::2], nums[1::2])


def test_analyze_groups_by_colour_and_treats_unstroked_as_black(sample_svg):
    infos = analyze_svg(sample_svg)
    colors = [i.color for i in infos]
    assert colors[0] == "#000000"                      # black first
    assert set(colors) == {"#000000", "#ff0000", "#0000ff", "#00ff00"}
    black = infos[0]
    assert black.lines == 2                            # unstroked line + stroke="#000" rect
    assert all(i.length_mm > 0 for i in infos)


def test_convert_maps_colours_to_pens_and_parks(sample_svg):
    result = convert_svg(
        sample_svg,
        {"#ff0000": 2, "#0000ff": 3, "#00ff00": 3, "#000000": 1},
        {1: 10, 2: 5, 3: 8},
        {1: "#000000", 2: "#ff0000", 3: "#0000ff"},
        PlotOptions(area=Rect(0, 0, 420, 297)),
    )
    cmds = result.commands
    assert result.pen_numbers == [1, 2, 3]             # ordered by pen, blue+green merged into 3
    assert cmds[-2].startswith("PU") and cmds[-1] == "SP0;"
    assert "IN;" not in cmds[1:]                       # only the leading IN survives
    # speed follows each pen selection
    assert cmds[cmds.index("SP2;") + 1] == "VS5;"
    assert cmds[cmds.index("SP3;") + 1] == "VS8;"
    assert cmds.count("SP3;") == 1                     # one group per pen, not per colour
    # every coordinate inside the bed
    xs, ys = zip(*_coords(cmds))
    assert 0 <= min(xs) and max(xs) <= 16700
    assert 0 <= min(ys) and max(ys) <= 11800
    assert result.rotated                              # portrait SVG onto a landscape sheet
    assert result.estimated_seconds > 0
    assert len(result.pens) == 3 and {p.pen for p in result.pens} == {1, 2, 3}


def test_preview_is_inline_svg(sample_svg):
    result = convert_svg(sample_svg, {}, {}, options=PlotOptions())
    assert result.preview_svg.startswith("<svg")
    assert "<?xml" not in result.preview_svg


def test_unmapped_colours_fall_back_to_default_pen(sample_svg):
    result = convert_svg(sample_svg, {"#ff0000": 4}, {}, options=PlotOptions(), default_pen=1)
    assert result.pen_numbers == [1, 4]
    assert result.commands[result.commands.index("SP1;") + 1].startswith("VS")


def _drawn_coords(commands):
    """Coordinates of drawing moves only (skip the final park)."""
    return list(_coords([c for c in commands if c != "PU0,0;"]))


def test_a4_fit_uses_the_whole_sheet(sample_svg):
    """Regression: the top of a portrait page used to be cropped away by the device config."""
    result = convert_svg(sample_svg, {}, {}, options=PlotOptions(area=A4, fit=True, margin_mm=10))
    xs, ys = zip(*_drawn_coords(result.commands))
    assert 0 <= min(xs) and max(xs) <= 297 / 0.025
    assert 0 <= min(ys) and max(ys) <= 210 / 0.025
    # the drawing spans the sheet minus margins, so nothing was cut off
    assert result.cropped_fraction == 0 and not result.warnings
    assert abs(result.bounds.x - 10) < 0.5 and abs(result.bounds.x2 - 287) < 0.5
    assert result.bounds.y2 <= 200.5 and result.bounds.h > 150


def test_no_rotation_option_keeps_portrait(sample_svg):
    result = convert_svg(sample_svg, {}, {}, options=PlotOptions(area=A4, fit=True, margin_mm=10, rotation="none"))
    assert not result.rotated
    assert abs(result.bounds.y - 10) < 0.5 and abs(result.bounds.y2 - 200) < 0.5   # fills the height instead
    assert result.bounds.w < result.bounds.h


def test_area_offset_moves_drawing_on_bed(sample_svg):
    card = Rect(100, 50, 152.4, 101.6)
    result = convert_svg(sample_svg, {}, {}, options=PlotOptions(area=card, fit=True, margin_mm=5))
    b = result.bounds
    assert b.x >= 100 + 4.5 and b.x2 <= card.x2 - 4.5
    assert b.y >= 50 + 4.5 and b.y2 <= card.y2 - 4.5
    xs, ys = zip(*_drawn_coords(result.commands))
    assert min(xs) >= 100 / 0.025 and min(ys) >= 50 / 0.025
    x0, y0, x1, y1 = result.bounds_units
    assert x0 <= min(xs) and x1 >= max(xs) and y0 <= min(ys) and y1 >= max(ys)


def test_keep_size_crops_to_area_with_warning(sample_svg):
    # A6 card is far smaller than the A4 SVG: keep scale, so most of it falls outside.
    result = convert_svg(sample_svg, {}, {}, options=PlotOptions(area=Rect(0, 0, 148, 105), fit=False, rotation="none"))
    assert result.scale == 1.0
    assert result.cropped_fraction > 0.3
    assert any("outside the plot area" in w for w in result.warnings)
    xs, ys = zip(*_drawn_coords(result.commands))
    assert max(xs) <= 148 / 0.025 + 1 and max(ys) <= 105 / 0.025 + 1


def test_area_clamped_to_bed():
    assert BED == Rect(0, 0, BED_W_MM, BED_H_MM)
    big = Rect(0, 0, 420, 297).clamped_to_bed()
    assert (big.w, big.h) == (BED_W_MM, BED_H_MM)
    off = Rect(400, 280, 100, 100).clamped_to_bed()
    assert off.x2 <= BED_W_MM and off.y2 <= BED_H_MM
    assert Rect(10, 20, 30, 40).to_units() == (400, 800, 1600, 2400)
    assert Rect.from_units(400, 800, 1600, 2400) == Rect(10, 20, 30, 40)


def test_preview_shows_bed_and_area(sample_svg):
    result = convert_svg(sample_svg, {}, {}, options=PlotOptions(area=A4))
    assert 'id="bed"' in result.preview_svg
    assert result.preview_svg.count("<rect") >= 3     # bed, area, bounds


def test_plot_options_from_form():
    o = PlotOptions.from_form({"area_x": "5", "area_y": "7", "area_w": "100", "area_h": "150",
                               "fit": "keep", "margin_mm": "7.5", "rotation": "rotate"})
    assert o.area == Rect(5, 7, 100, 150)
    assert (o.fit, o.margin_mm, o.rotation) == (False, 7.5, "rotate")
    o = PlotOptions.from_form({"area_w": "x", "margin_mm": "nan", "rotation": "sideways"})
    assert o.area == Rect(0, 0, 297, 210)
    assert (o.fit, o.margin_mm, o.rotation) == (True, 10.0, "auto")
    assert area_from_form({"area_x": "-5", "area_w": "0"}) == Rect(0, 0, 1, 210)


def test_format_duration():
    assert format_duration(42) == "42s"
    assert format_duration(125) == "2m 05s"
    assert format_duration(3700) == "1h 01m"
