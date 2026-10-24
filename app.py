"""
PlotterDash: Flask web UI for driving an HPGL pen plotter over serial.
"""
from __future__ import annotations

import json
import logging
import os
import time
import uuid

from flask import Flask, Response, render_template, request, make_response
from serial.tools import list_ports
from werkzeug.utils import secure_filename

from hpgl_utils import jog_commands, outline_commands, pen_test_commands
from pen_library import PenLibrary, PEN_COUNT, MAX_SPEED, clamp_speed, normalize_hex
from plotter_sim import SIM_PORT
from serial_worker import SerialWorker
from svg_convert import (
    BED, DEFAULT_PRESET, PAPER_PRESETS, PLOTTER_UNIT_MM, ROTATIONS, PlotOptions, Rect,
    analyze_svg, area_from_form, convert_svg, format_duration,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("plotterdash")

HERE = os.path.dirname(os.path.abspath(__file__))
SIMULATE = os.environ.get("PLOTTERDASH_SIMULATE", "").lower() in ("1", "true", "yes")

app = Flask(__name__)
app.config["UPLOAD_FOLDER"] = os.path.join(HERE, "uploads")
app.config["MAX_CONTENT_LENGTH"] = 50 * 1024 * 1024
os.makedirs(app.config["UPLOAD_FOLDER"], exist_ok=True)

serial_worker = SerialWorker(simulate=SIMULATE)
pen_library = PenLibrary(os.path.join(HERE, "pens.json"))

app.jinja_env.filters["duration"] = format_duration


# ------------------------------------------------------------------ helpers
def toast(message: str, kind: str = "info", body: str = "", status: int = 200, **events):
    """Response that asks the page to show a toast (and fire any extra client events)."""
    resp = make_response(body, status)
    payload = {"toast": {"message": message, "type": kind}}
    payload.update(events)
    resp.headers["HX-Trigger"] = json.dumps(payload)
    return resp


def error(message: str, status: int = 400):
    return make_response(message, status)


def available_ports() -> list[str]:
    ports = [p.device for p in list_ports.comports()]
    if SIMULATE:
        ports.insert(0, SIM_PORT)
    return ports


def prune_uploads(max_age_seconds: float = 24 * 3600) -> None:
    """Delete uploaded SVGs older than a day; queued jobs keep their commands in memory."""
    cutoff = time.time() - max_age_seconds
    try:
        for name in os.listdir(app.config["UPLOAD_FOLDER"]):
            path = os.path.join(app.config["UPLOAD_FOLDER"], name)
            if name.endswith(".svg") and os.path.isfile(path) and os.path.getmtime(path) < cutoff:
                os.remove(path)
    except OSError:
        pass


def stored_svg_path(stored_name: str | None) -> str | None:
    """Resolve a stored upload name to a path inside the upload folder, or None."""
    if not stored_name:
        return None
    safe = secure_filename(stored_name)
    if safe != stored_name:
        return None
    path = os.path.join(app.config["UPLOAD_FOLDER"], safe)
    return path if os.path.isfile(path) else None


def position_payload(pos: tuple[int, int, bool]) -> dict:
    x, y, pen_down = pos
    return {
        "x": x, "y": y,
        "x_mm": round(x * PLOTTER_UNIT_MM, 2), "y_mm": round(y * PLOTTER_UNIT_MM, 2),
        "pen_down": pen_down,
    }


def json_response(payload: dict, status: int = 200, **events):
    resp = app.response_class(json.dumps(payload), status=status, mimetype="application/json")
    if events:
        resp.headers["HX-Trigger"] = json.dumps(events)
    return resp


def area_label(area: Rect) -> str:
    return f"{area.w:.0f} x {area.h:.0f} mm at ({area.x:.0f}, {area.y:.0f})"


def parse_assignment(form) -> tuple[dict[str, int], dict[int, int]]:
    """Read pen_<hex> / speed_<hex> fields into colour->pen and pen->speed maps."""
    color_to_pen: dict[str, int] = {}
    pen_speeds: dict[int, int] = {}
    for key, value in form.items():
        if key.startswith("pen_"):
            color = normalize_hex(key[4:])
            try:
                pen = int(value)
            except ValueError:
                continue
            if 1 <= pen <= PEN_COUNT:
                color_to_pen[color] = pen
    for color, pen in color_to_pen.items():
        raw = form.get(f"speed_{color}")
        pen_speeds[pen] = clamp_speed(raw) if raw else pen_library.get(pen)["speed"]
    return color_to_pen, pen_speeds


# ------------------------------------------------------------------ pages
@app.route("/")
def index():
    return render_template(
        "index.html",
        port_list=available_ports(),
        pens=pen_library.all(),
        paper_presets=PAPER_PRESETS,
        default_preset=DEFAULT_PRESET,
        rotations=ROTATIONS,
        bed=BED,
        simulate=SIMULATE,
        max_speed=MAX_SPEED,
    )


@app.route("/ports")
def ports():
    return render_template("_port_options.html", port_list=available_ports())


# ------------------------------------------------------------------ connection
@app.route("/connect", methods=["POST"])
def connect_serial():
    port = request.form.get("port", "").strip()
    try:
        baud = int(request.form.get("baud", ""))
    except ValueError:
        return error("Invalid baud rate")
    if not port:
        return error("Choose a port first")
    ok, message = serial_worker.connect(port, baud)
    if not ok:
        return error(message)
    return "", 204


@app.route("/disconnect", methods=["POST"])
def disconnect_serial():
    serial_worker.disconnect()
    return "", 204


# ------------------------------------------------------------------ upload / assign / queue
@app.route("/analyze", methods=["POST"])
def analyze():
    svg_file = request.files.get("svg_file")
    if svg_file is None or not svg_file.filename:
        return error("Choose an SVG file first")

    prune_uploads()
    original = secure_filename(svg_file.filename) or "upload.svg"
    stored = f"{uuid.uuid4().hex[:8]}_{original}"
    path = os.path.join(app.config["UPLOAD_FOLDER"], stored)
    svg_file.save(path)

    try:
        colors = analyze_svg(path)
    except Exception as e:  # svgelements / vpype parse failures
        log.exception("Failed to analyze %s", original)
        os.remove(path)
        return error(f"Could not read SVG: {e}")

    if not colors:
        os.remove(path)
        return error("No plottable paths found in this SVG")

    rows = []
    for info in colors:
        pen = pen_library.suggest(info.color)
        rows.append({
            "color": info.color,
            "lines": info.lines,
            "length_mm": info.length_mm,
            "pen": pen,
            "speed": pen_library.get(pen)["speed"],
        })

    return render_template(
        "_assignment.html",
        filename=original,
        stored=stored,
        rows=rows,
        pens=pen_library.all(),
        rotations=ROTATIONS,
        max_speed=MAX_SPEED,
    )


def _convert_from_form(form):
    path = stored_svg_path(form.get("stored"))
    if not path:
        return None, error("Uploaded file not found. Please choose it again.")
    color_to_pen, pen_speeds = parse_assignment(form)
    options = PlotOptions.from_form(form)
    try:
        result = convert_svg(path, color_to_pen, pen_speeds, pen_library.colors(), options)
    except Exception as e:
        log.exception("Conversion failed for %s", path)
        return None, error(f"Conversion failed: {e}", 500)
    return (result, color_to_pen, pen_speeds, path), None


@app.route("/preview", methods=["POST"])
def preview():
    data, err = _convert_from_form(request.form)
    if err:
        return err
    result, _, _, _ = data
    return render_template("_preview.html", result=result, pens=pen_library.pens, bed=BED)


@app.route("/queue", methods=["POST"])
def queue_job():
    data, err = _convert_from_form(request.form)
    if err:
        return err
    result, color_to_pen, pen_speeds, path = data
    filename = request.form.get("filename") or os.path.basename(path)
    pen_library.remember(color_to_pen)
    serial_worker.enqueue_job(
        filename, result.commands, result.pen_numbers, pen_speeds,
        result.estimated_seconds, preview_svg=result.preview_svg,
    )
    # Clear the assignment card (target) and the preview (out of band).
    return toast(f"Queued {filename}", "success",
                 body='<div id="preview" hx-swap-oob="innerHTML"></div>', jobQueued=True)


@app.route("/outline", methods=["POST"])
def outline_job():
    """Queue a pen-up trace of the plot area and the drawing's extent, so the placement can be checked on the bed."""
    data, err = _convert_from_form(request.form)
    if err:
        return err
    result, _, _, path = data
    filename = request.form.get("filename") or os.path.basename(path)
    rects = [result.area.to_units()]
    if result.bounds is not None:
        rects.append(result.bounds.to_units())
    commands = outline_commands(rects)
    serial_worker.enqueue_job(f"Outline: {filename}", commands, [], {}, estimated_seconds=8 * len(rects), kind="outline")
    return toast(f"Outline trace queued ({area_label(result.area)})", "success")


@app.route("/trace_area", methods=["POST"])
def trace_area():
    """Queue a pen-up trace of the plot area alone."""
    area = area_from_form(request.form).clamped_to_bed()
    commands = outline_commands([area.to_units()], passes=2)
    serial_worker.enqueue_job(f"Area outline {area.w:.0f} x {area.h:.0f} mm", commands, [], {},
                              estimated_seconds=10, kind="outline")
    return toast(f"Area trace queued ({area_label(area)})", "success")


# ------------------------------------------------------------------ queue management
@app.route("/jobs/<job_id>/cancel", methods=["POST"])
def cancel_job(job_id):
    if not serial_worker.cancel_job(job_id):
        return error("Job cannot be cancelled", 404)
    return "", 204


@app.route("/jobs/<job_id>/remove", methods=["POST"])
def remove_job(job_id):
    if not serial_worker.remove_job(job_id):
        return error("Job cannot be removed", 404)
    return "", 204


@app.route("/jobs/<job_id>/rerun", methods=["POST"])
def rerun_job(job_id):
    if not serial_worker.rerun_job(job_id):
        return error("Job not found", 404)
    return toast("Job re-queued", "success")


@app.route("/jobs/<job_id>/move/<direction>", methods=["POST"])
def move_job(job_id, direction):
    if direction not in ("up", "down"):
        return error("Bad direction")
    serial_worker.move_job(job_id, direction)
    return "", 204


@app.route("/jobs/<job_id>/preview")
def job_preview(job_id):
    job = serial_worker.get_job(job_id)
    if not job or not job.get("preview_svg"):
        return error("No preview for this job", 404)
    return render_template("_job_preview.html", job=job)


@app.route("/jobs/clear_finished", methods=["POST"])
def clear_finished():
    n = serial_worker.clear_finished()
    return toast(f"Cleared {n} finished job{'s' if n != 1 else ''}")


@app.route("/queue/hold", methods=["POST"])
def hold_queue():
    held = request.form.get("held", "1") == "1"
    serial_worker.set_queue_held(held)
    return "", 204


# ------------------------------------------------------------------ job control
@app.route("/pause", methods=["POST"])
def pause():
    if not serial_worker.pause_current_job():
        return error("No job is running")
    return "", 204


@app.route("/resume", methods=["POST"])
def resume():
    if not serial_worker.resume_current_job():
        return error("No job is running")
    return "", 204


@app.route("/stop", methods=["POST"])
def stop():
    if not serial_worker.stop_current_job():
        return error("No job is running")
    return "", 204


# ------------------------------------------------------------------ manual control
def _require_idle():
    if not serial_worker.is_connected:
        return error("Not connected")
    if serial_worker.current_job_id:
        return error("A job is running. Pause or stop it first.")
    return None


@app.route("/send_command", methods=["POST"])
def send_command():
    blocked = _require_idle()
    if blocked:
        return blocked
    cmd = request.form.get("command", "").strip()
    if not cmd:
        return error("No command provided")
    serial_worker.send_custom_command(cmd)
    return "", 204


@app.route("/jog", methods=["POST"])
def jog():
    blocked = _require_idle()
    if blocked:
        return blocked
    direction = request.form.get("direction", "")
    if direction not in ("up", "down", "left", "right"):
        return error("Bad direction")
    try:
        distance = max(1, min(20000, int(request.form.get("distance", 100))))
    except ValueError:
        return error("Bad distance")
    pen_down = request.form.get("pen_down") == "on"
    serial_worker.send_custom_command(jog_commands(direction, distance, pen_down))
    pos = serial_worker.read_position()
    if pos is None:
        return "", 204
    payload = position_payload(pos)
    return json_response(payload, positionChanged=payload)


@app.route("/position", methods=["POST"])
def position():
    """Ask the plotter where the pen is (``OA;``)."""
    blocked = _require_idle()
    if blocked:
        return blocked
    pos = serial_worker.read_position()
    if pos is None:
        return error("The plotter did not report its position")
    payload = position_payload(pos)
    return json_response(payload, positionChanged=payload)


@app.route("/limits", methods=["POST"])
def limits():
    """Ask the plotter for its hard-clip limits (``OH;``) and compare with the configured bed."""
    blocked = _require_idle()
    if blocked:
        return blocked
    lim = serial_worker.read_limits()
    if lim is None:
        return error("The plotter did not report its limits")
    x0, y0, x1, y1 = lim
    reported = Rect.from_units(x0, y0, x1, y1)
    payload = {
        "x0": x0, "y0": y0, "x1": x1, "y1": y1,
        "w_mm": round(reported.w, 1), "h_mm": round(reported.h, 1),
        "bed_w_mm": round(BED.w, 1), "bed_h_mm": round(BED.h, 1),
        "matches": abs(reported.w - BED.w) < 1 and abs(reported.h - BED.h) < 1 and x0 == 0 and y0 == 0,
    }
    return json_response(payload)


@app.route("/pen_test", methods=["POST"])
def pen_test():
    pens = []
    for value in request.form.getlist("pens"):
        try:
            n = int(value)
        except ValueError:
            continue
        if 1 <= n <= PEN_COUNT and n not in pens:
            pens.append(n)
    if not pens:
        return error("Select at least one pen")
    speeds = {n: pen_library.get(n)["speed"] for n in pens}
    commands = pen_test_commands(pens, pen_speeds=speeds)
    serial_worker.enqueue_job("Pen test", commands, pens, speeds, estimated_seconds=6 * len(pens), kind="pen_test")
    return toast(f"Pen test queued for pen{'s' if len(pens) > 1 else ''} {', '.join(map(str, pens))}", "success")


# ------------------------------------------------------------------ pen library
@app.route("/pens", methods=["GET"])
def pens_form():
    return render_template("_pens_form.html", pens=pen_library.all(), max_speed=MAX_SPEED)


@app.route("/pens", methods=["POST"])
def pens_save():
    pen_library.update_from_form(request.form)
    return toast("Pen library saved", "success",
                 body=render_template("_pens_form.html", pens=pen_library.all(), max_speed=MAX_SPEED),
                 pensChanged=True)


@app.route("/pens/json")
def pens_json():
    return app.response_class(json.dumps(pen_library.all()), mimetype="application/json")


# ------------------------------------------------------------------ status stream
@app.route("/job_status_stream")
def job_status_stream():
    def event_stream():
        last = None
        last_sent = 0.0
        while True:
            snapshot = serial_worker.get_status_snapshot()
            if snapshot["position"]:
                snapshot["position"] = position_payload(
                    (snapshot["position"]["x"], snapshot["position"]["y"], snapshot["position"]["pen_down"]))
            payload = json.dumps(snapshot)
            now = time.time()
            if payload != last or now - last_sent > 10:
                yield f"data: {payload}\n\n"
                last, last_sent = payload, now
            time.sleep(0.5)

    return Response(event_stream(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


if __name__ == "__main__":
    log.info("Starting PlotterDash%s", " (simulated plotter enabled)" if SIMULATE else "")
    app.run(debug=True, threaded=True, use_reloader=False)
