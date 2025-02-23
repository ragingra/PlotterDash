import os
import time

from serial.tools import list_ports

from hpgl_utils import svg_to_hpgl_commands
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
        return "Invalid baud", 400

    serial_worker.connect(port, baud)
    return redirect(url_for("index"))

@app.route("/disconnect", methods=["POST"])
def disconnect_serial():
    serial_worker.disconnect()
    return redirect(url_for("index"))

# ------------------------------
# Upload/Queue Jobs
# ------------------------------
@app.route("/upload_svg", methods=["POST"])
def upload_svg():
    """
    Handle multiple SVG uploads in one request if desired.
    Also handle pen_speed, pen_number from the form (both default to 1).
    """
    if "svg_file" not in request.files:
        return "No SVG file part", 400

    pen_speed = int(request.form.get("pen_speed", 1))
    pen_number = int(request.form.get("pen_number", 1))

    svg_files = request.files.getlist("svg_file")
    if not svg_files:
        return "No files selected", 400

    # For each file, generate an individual job
    for f in svg_files:
        if f.filename == "":
            continue
        filename = f.filename

        # Save the file
        filepath = os.path.join(app.config["UPLOAD_FOLDER"], filename)
        f.save(filepath)

        # Convert SVG to HPGL commands
        commands = svg_to_hpgl_commands(filepath, pen_speed, pen_number)

        # Enqueue the job
        serial_worker.enqueue_job(filename, commands, pen_speed, pen_number)

    return redirect(url_for("index"))

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
