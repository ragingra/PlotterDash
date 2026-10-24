# PlotterDash

Flask web UI for controlling an HPGL pen plotter (Roland DXY-1200) over serial.

<img width="819" height="652" alt="Screenshot 2026-01-05 at 21 43 36" src="https://github.com/user-attachments/assets/c051565d-22cc-4514-bb3b-9a3f60d81c44" />

Upload SVGs, assign each stroke colour to a carousel pen with its own speed, preview the
laid-out plot with a time estimate, and queue jobs to run one after another. Converts and
optimises SVG to HPGL with [vpype](https://github.com/abey79/vpype).

https://github.com/user-attachments/assets/3425bd3b-f8bf-4849-ac8b-0a2d40692c38

![PXL_20250107_210321327](https://github.com/user-attachments/assets/685dfea5-d8de-4cd2-ae77-3713bd0545e5)

## Features

- **Multi-pen plotting**: strokes are grouped by colour (unstroked paths count as black),
  each colour is mapped to a pen slot, colours sharing a pen are plotted together, and
  pen changes are emitted in slot order with a per-pen `VS` speed.
- **Pen library**: name, colour and default speed for each of the 8 slots, saved in
  `pens.json`. Colour-to-pen choices are remembered and suggested next time.
- **Preview before plotting**: the whole bed with the plot area and the drawing on it,
  coloured per pen, with pen-down distance, scale and an estimated plot time. Anything
  that would fall outside the plot area is flagged.
- **Plot area**: paper presets (A3 to A6, cards, DL, whole bed) or a custom size, placed
  anywhere on the bed by its bottom-left corner. Both corners can be captured from the
  pen position after jogging to them. Fit with a margin or keep the SVG size; rotate
  automatically to match the area, never, or always.
- **Dry run**: "Trace outline" moves the empty pen holder, pen up, around the plot area
  and around the drawing's extent, so placement can be checked before a single line is
  drawn. "Check limits" asks the plotter (`OH;`) for its real plotting range.
- **Queue management**: queue while offline, hold the queue, reorder, cancel, re-run,
  remove, clear finished. Pause lifts the pen; stop lifts the pen and parks it (`SP0`).
- **Manual control**: relative jog pad with live pen position (`OA;`), home, pen
  up/down, park, select pen, pen test (one swatch per pen), free-form HPGL with the
  plotter's reply.
- **Simulator**: run without hardware for development.

## Setup

```bash
python3.12 -m venv env
./env/bin/pip install -r requirements.txt
./env/bin/python app.py
```

Open http://127.0.0.1:5000. Plotter settings (serial parameters, plotting range) live in
`printerconfig/roland_dxy1200.toml`.

### Coordinates

Everything is laid out in the plotter's own frame: X along the long edge of the bed, Y
along the short edge, origin at the bottom-left (DXY "lower-left origin" mode). The bed
size is taken from `x_range` / `y_range` in the device config (16700 x 11800 units =
417.5 x 295 mm). Load paper with its long edge along X and its bottom-left corner where
you want the plot area to start; for anything smaller than the bed, jog the pen to the
sheet's bottom-left corner and press "Bottom-left = pen" in the Plot area panel (and
optionally "Top-right = pen" for the far corner). The preview shows exactly where on
the bed the drawing will land. If "Check limits" reports a range different from the
configured bed, update `x_range` / `y_range` in the config.

To develop without a plotter:

```bash
PLOTTERDASH_SIMULATE=1 ./env/bin/python app.py
```

and connect to the `SIM` port.

## Tests

```bash
./env/bin/pip install pytest
./env/bin/python -m pytest tests
```

## Layout

| File | Purpose |
| --- | --- |
| `app.py` | Flask routes |
| `svg_convert.py` | SVG analysis and SVG → HPGL conversion via vpype |
| `hpgl_utils.py` | HPGL text helpers: chunking, speed injection, parking, jog, pen test |
| `serial_worker.py` | Serial connection, job queue and the sending thread |
| `pen_library.py` | Pen slot descriptions and colour memory (`pens.json`) |
| `plotter_sim.py` | Fake serial port that answers like a DXY-1200 |
| `templates/` | Bootstrap + HTMX + Alpine UI |
