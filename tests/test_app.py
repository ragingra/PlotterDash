import json
import os
import re

import pytest


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("PLOTTERDASH_SIMULATE", "1")
    import importlib
    import app as app_module
    importlib.reload(app_module)
    app_module.app.config["UPLOAD_FOLDER"] = str(tmp_path / "uploads")
    os.makedirs(app_module.app.config["UPLOAD_FOLDER"], exist_ok=True)
    app_module.pen_library.path = str(tmp_path / "pens.json")
    app_module.serial_worker.command_delay = 0.0
    app_module.app.config["TESTING"] = True
    with app_module.app.test_client() as c:
        yield c, app_module
    app_module.serial_worker.disconnect()


def test_index_renders(client):
    c, _ = client
    r = c.get("/")
    assert r.status_code == 200
    assert b"PlotterDash" in r.data and b"SIM" in r.data


def test_connect_errors_are_plain_text_400(client):
    c, _ = client
    r = c.post("/connect", data={"port": "SIM", "baud": "abc"})
    assert r.status_code == 400 and b"Invalid baud" in r.data
    r = c.post("/connect", data={"port": "", "baud": "9600"})
    assert r.status_code == 400
    r = c.post("/jog", data={"direction": "up", "distance": "10"})
    assert r.status_code == 400 and b"Not connected" in r.data


def test_full_flow_analyze_preview_queue(client, sample_svg):
    c, mod = client
    with open(sample_svg, "rb") as f:
        r = c.post("/analyze", data={"svg_file": (f, "sample.svg")}, content_type="multipart/form-data")
    assert r.status_code == 200
    html = r.data.decode()
    stored = re.search(r'name="stored" value="([^"]+)"', html).group(1)
    assert "#ff0000" in html and "#000000" in html

    form = {
        "stored": stored, "filename": "sample.svg",
        "pen_#000000": "1", "speed_#000000": "10",
        "pen_#ff0000": "2", "speed_#ff0000": "6",
        "pen_#0000ff": "2", "speed_#0000ff": "6",
        "pen_#00ff00": "4", "speed_#00ff00": "12",
        "area_x": "0", "area_y": "0", "area_w": "420", "area_h": "297", "fit": "fit", "margin_mm": "10", "rotation": "auto",
    }
    r = c.post("/preview", data=form)
    assert r.status_code == 200 and b"<svg" in r.data and b"Est. time" in r.data

    r = c.post("/queue", data=form)
    assert r.status_code == 200
    trigger = json.loads(r.headers["HX-Trigger"])
    assert trigger["toast"]["type"] == "success" and trigger["jobQueued"] is True
    assert b'hx-swap-oob' in r.data

    snap = mod.serial_worker.get_status_snapshot()
    assert len(snap["jobs"]) == 1
    job = snap["jobs"][0]
    assert job["status"] == "queued" and job["pens"] == [1, 2, 4]
    assert job["pen_speeds"] == {"1": 10, "2": 6, "4": 12}
    assert mod.pen_library.suggest("#0000ff") == 2  # mapping remembered

    r = c.get(f"/jobs/{job['job_id']}/preview")
    assert r.status_code == 200 and b"<svg" in r.data

    r = c.post("/queue", data={**form, "stored": "../app.py"})
    assert r.status_code == 400

    # Outline trace of the same job: pen-up moves around the area and the drawing, no pen selected.
    r = c.post("/outline", data=form)
    assert r.status_code == 200
    jobs = mod.serial_worker.get_status_snapshot()["jobs"]
    outline = next(j for j in jobs if j["kind"] == "outline")
    assert outline["filename"].startswith("Outline:") and outline["pens"] == []
    cmds = mod.serial_worker.get_job(outline["job_id"])["commands"]
    assert "SP0;" in cmds and all(not c.startswith("PD") for c in cmds) and cmds[-1] == "SP0;"


def test_trace_area_and_position_routes(client):
    c, mod = client
    r = c.post("/trace_area", data={"area_x": "10", "area_y": "20", "area_w": "100", "area_h": "50"})
    assert r.status_code == 200
    job = mod.serial_worker.get_status_snapshot()["jobs"][0]
    assert job["kind"] == "outline"
    cmds = mod.serial_worker.get_job(job["job_id"])["commands"]
    assert "PU400,800;" in cmds and "PU4400,2800;" in cmds

    assert c.post("/position").status_code == 400          # not connected
    assert c.post("/connect", data={"port": "SIM", "baud": "9600"}).status_code == 204
    mod.serial_worker.set_queue_held(True)                  # keep the outline job from running

    r = c.post("/jog", data={"direction": "right", "distance": "400"})
    assert r.status_code == 200
    assert r.json["x"] == 400 and r.json["x_mm"] == 10.0
    assert json.loads(r.headers["HX-Trigger"])["positionChanged"]["x"] == 400

    r = c.post("/position")
    assert r.status_code == 200 and r.json == {"x": 400, "y": 0, "x_mm": 10.0, "y_mm": 0.0, "pen_down": False}

    r = c.post("/limits")
    assert r.status_code == 200 and r.json["matches"] is True and r.json["w_mm"] == 417.5


def test_pen_routes(client):
    c, mod = client
    r = c.post("/pens", data={"name_1": "Micron", "color_1": "#112233", "speed_1": "7"})
    assert r.status_code == 200 and b"Micron" in r.data
    assert json.loads(r.headers["HX-Trigger"])["pensChanged"] is True
    r = c.get("/pens/json")
    assert r.json[0] == {"slot": 1, "name": "Micron", "color": "#112233", "speed": 7}
    r = c.post("/pen_test", data={"pens": ["1", "9", "x"]})
    assert r.status_code == 200
    assert mod.serial_worker.get_status_snapshot()["jobs"][0]["kind"] == "pen_test"
    r = c.post("/pen_test", data={})
    assert r.status_code == 400


def test_job_control_requires_running_job(client):
    c, _ = client
    assert c.post("/pause").status_code == 400
    assert c.post("/stop").status_code == 400
    assert c.post("/jobs/nope/remove").status_code == 404
