import os
import time

from serial.tools import list_ports

from hpgl_utils import svg_to_hpgl_commands, svg_to_hpgl_commands_multicolor
from color_utils import extract_colors_from_svg
from serial_worker import SerialWorker

from flask import Flask, render_template, request, redirect, url_for, jsonify, Response


# -------------------------------------------------------------------
# FLASK APP
# -------------------------------------------------------------------
app = Flask(__name__)
app.config["UPLOAD_FOLDER"] = "uploads"
os.makedirs(app.config["UPLOAD_FOLDER"], exist_ok=True)

# Single global worker instance
serial_worker = SerialWorker()

@app.route("/")
def index():
    """
    Main page with:
      - Connect/Disconnect
      - Upload multiple SVG files (plus pen speed & pen number)
      - Pause/Resume/Stop
      - Real-time job status
    """

    available_ports = [p.device for p in list_ports.comports()]

    print("Available ports:", available_ports)

    return render_template("index.html", port_list=available_ports)

# ------------------------------
# Serial Connection Routes
# ------------------------------
@app.route("/connect", methods=["POST"])
def connect_serial():
    port = request.form.get("port")
    baud = request.form.get("baud")
    try:
        baud = int(baud)
    except:
        return "<div class='alert alert-danger'>Invalid baud rate</div>", 400

    serial_worker.connect(port, baud)

    if serial_worker.is_connected:
        return """
        <div class="d-flex align-items-center gap-3">
            <span class="badge bg-success">Connected</span>
            <form hx-post="/disconnect" hx-target="#connection-status" hx-swap="innerHTML">
                <button class="btn btn-warning btn-sm">Disconnect</button>
            </form>
        </div>
        """
    else:
        return "<div class='alert alert-danger'>Failed to connect. Check port and try again.</div>", 400


@app.route("/disconnect", methods=["POST"])
def disconnect_serial():
    serial_worker.disconnect()
    return "<div class='text-muted'>Disconnected</div>"

# ------------------------------
# Upload/Queue Jobs
# ------------------------------
@app.route("/analyze_svg", methods=["POST"])
def analyze_svg():
    """
    Analyze an SVG file and return color assignment UI.
    """
    if "svg_file" not in request.files:
        return "<div class='alert alert-danger'>No SVG file provided</div>", 400

    svg_file = request.files["svg_file"]
    if svg_file.filename == "":
        return "<div class='alert alert-danger'>No file selected</div>", 400

    pen_speed = int(request.form.get("pen_speed", 1))

    # Save the file
    filename = svg_file.filename
    filepath = os.path.join(app.config["UPLOAD_FOLDER"], filename)
    svg_file.save(filepath)

    # Extract colors from SVG
    colors = extract_colors_from_svg(filepath)

    # Return color assignment partial
    return render_template(
        "_color_assignment.html",
        filename=filename,
        filepath=filepath,
        colors=colors,
        pen_speed=pen_speed
    )


@app.route("/upload_svg", methods=["POST"])
def upload_svg():
    """
    Queue an SVG job with color-to-pen mapping.
    """
    filepath = request.form.get("filepath")
    filename = request.form.get("filename")
    pen_speed = int(request.form.get("pen_speed", 1))

    if not filepath or not os.path.exists(filepath):
        return "<div class='alert alert-danger'>File not found</div>", 400

    # Build color-to-pen mapping from form data
    color_to_pen = {}
    for key, value in request.form.items():
        if key.startswith("pen_#"):
            color = key[4:]  # Remove "pen_" prefix
            color_to_pen[color.upper()] = int(value)

    # Convert SVG to HPGL with color-aware processing
    if color_to_pen:
        commands = svg_to_hpgl_commands_multicolor(filepath, color_to_pen, pen_speed)
        # Use first pen number for job display
        pen_number = list(color_to_pen.values())[0] if color_to_pen else 1
    else:
        commands = svg_to_hpgl_commands(filepath, pen_speed, 1)
        pen_number = 1

    # Enqueue the job
    serial_worker.enqueue_job(filename, commands, pen_speed, pen_number)

    # Return success message that clears the color preview
    return """
    <div class="alert alert-success alert-dismissible fade show" role="alert">
        Job queued successfully!
        <button type="button" class="btn-close" data-bs-dismiss="alert"></button>
    </div>
    """

# ------------------------------
# Pause/Resume/Stop
# ------------------------------
@app.route("/pause", methods=["POST"])
def pause():
    serial_worker.pause_current_job()

    return "ok", 200


@app.route("/resume", methods=["POST"])
def resume():
    serial_worker.resume_current_job()

    return "ok", 200

@app.route("/stop", methods=["POST"])
def stop():
    serial_worker.stop_current_job()

    return "ok", 200

# ------------------------------
# Custom HPGL Command
# ------------------------------
@app.route("/send_command", methods=["POST"])
def send_command():
    """
    Send a custom HPGL command from the UI (outside the queued jobs).
    We'll return a short message or redirect. 
    """
    cmd = request.form.get("command", "").strip()
    if not cmd:
        return "No command provided", 400

    serial_worker.send_custom_command(cmd)

    return "ok", 200

# ------------------------------
# SSE Endpoint for Job Status
# ------------------------------
@app.route("/job_status_stream")
def job_status_stream():
    """
    SSE endpoint streaming a snapshot of the worker's state + all jobs.
    """
    def event_stream():
        while True:
            status_data = serial_worker.get_status_snapshot()
            import json
            yield f"data: {json.dumps(status_data)}\n\n"
            time.sleep(1)
    return Response(event_stream(), mimetype="text/event-stream")



# Optionally run
if __name__ == "__main__":
    app.run(debug=True)
