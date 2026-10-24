import time

import pytest

from plotter_sim import FakeSerial
from serial_worker import SerialWorker


def wait_for(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


@pytest.fixture
def worker():
    w = SerialWorker(simulate=True, command_delay=0.0, read_timeout=2.0)
    yield w
    w.disconnect()


def status(w, job_id):
    return w.jobs[job_id]["status"]


def test_connect_handshake_and_disconnect(worker):
    ok, msg = worker.connect("SIM", 9600)
    assert ok and worker.is_connected
    ok, msg = worker.connect("SIM", 9600)
    assert not ok and "Already" in msg
    worker.disconnect()
    assert not worker.is_connected


def test_connect_failure_for_bad_port():
    w = SerialWorker(simulate=True)
    ok, msg = w.connect("/dev/does-not-exist", 9600)
    assert not ok and "/dev/does-not-exist" in msg


def test_jobs_wait_for_connection_and_complete(worker):
    jid = worker.enqueue_job("a.svg", ["IN;", "SP1;", "PD1,1;", "PU;", "SP0;"], [1], {1: 10})
    time.sleep(0.3)
    assert status(worker, jid) == "queued"
    worker.connect("SIM", 9600)
    assert wait_for(lambda: status(worker, jid) == "done")
    assert worker.jobs[jid]["progress_percent"] == 100
    assert worker.get_status_snapshot()["jobs"][0]["status"] == "done"


def test_hold_queue(worker):
    worker.connect("SIM", 9600)
    worker.set_queue_held(True)
    jid = worker.enqueue_job("a.svg", ["PU;", "SP0;"], [], {})
    time.sleep(0.3)
    assert status(worker, jid) == "queued"
    worker.set_queue_held(False)
    assert wait_for(lambda: status(worker, jid) == "done")


def test_cancel_move_remove_rerun_clear(worker):
    worker.set_queue_held(True)
    a = worker.enqueue_job("a", ["PU;"], [], {})
    b = worker.enqueue_job("b", ["PU;"], [], {})
    c = worker.enqueue_job("c", ["PU;"], [], {})
    assert worker.queue == [a, b, c]
    assert worker.move_job(c, "up") and worker.queue == [a, c, b]
    assert not worker.move_job(a, "up")
    assert worker.cancel_job(b) and status(worker, b) == "cancelled" and b not in worker.queue
    assert worker.remove_job(b) and b not in worker.jobs
    assert not worker.remove_job("missing")
    new = worker.rerun_job(a)
    assert new in worker.queue and worker.jobs[new]["filename"] == "a"
    assert worker.clear_finished() == 0
    worker.connect("SIM", 9600)
    worker.set_queue_held(False)
    assert wait_for(lambda: all(j["status"] == "done" for j in worker.jobs.values()))
    assert worker.clear_finished() == 3 and worker.jobs == {}


def test_stop_lifts_pen_and_returns_it(worker):
    worker.connect("SIM", 9600)
    fake: FakeSerial = worker.serial_conn
    cmds = ["SP1;"] + [f"PD{i},{i};" for i in range(400)]
    jid = worker.enqueue_job("long", cmds, [1], {1: 10})
    assert wait_for(lambda: worker.jobs[jid]["current_index"] > 5)
    assert worker.stop_current_job()
    assert wait_for(lambda: status(worker, jid) == "stopped")
    tail = "".join(fake.sent[-2:])
    assert "PU;" in tail and "SP0;" in tail
    assert fake.pen == 0
    assert worker.current_job_id is None


def test_pause_lifts_pen_and_resume_continues(worker):
    worker.connect("SIM", 9600)
    fake: FakeSerial = worker.serial_conn
    cmds = ["SP1;"] + [f"PD{i},{i};" for i in range(400)]
    jid = worker.enqueue_job("long", cmds, [1], {1: 10})
    assert wait_for(lambda: worker.jobs[jid]["current_index"] > 5)
    assert worker.pause_current_job()
    assert wait_for(lambda: any(s.startswith("PU;") for s in fake.sent[-3:]))
    idx = worker.jobs[jid]["current_index"]
    time.sleep(0.3)
    assert worker.jobs[jid]["current_index"] == idx
    assert worker.get_status_snapshot()["is_paused"]
    assert worker.resume_current_job()
    assert wait_for(lambda: status(worker, jid) == "done")


def test_pause_stop_are_noops_when_idle(worker):
    assert not worker.pause_current_job()
    assert not worker.stop_current_job()
    assert not worker.resume_current_job()
    worker.connect("SIM", 9600)
    jid = worker.enqueue_job("a", ["PU;", "SP0;"], [], {})
    assert wait_for(lambda: status(worker, jid) == "done")


def test_transport_failure_marks_job_error(worker):
    worker.connect("SIM", 9600)
    fake: FakeSerial = worker.serial_conn
    cmds = ["SP1;"] + [f"PD{i},{i};" for i in range(400)]
    jid = worker.enqueue_job("long", cmds, [1], {1: 10})
    assert wait_for(lambda: worker.jobs[jid]["current_index"] > 5)
    worker.disconnect()
    assert wait_for(lambda: status(worker, jid) == "error")
    assert worker.jobs[jid]["error"] == "Not connected"


def test_plotter_error_codes_are_counted_not_fatal(worker):
    worker.connect("SIM", 9600)
    jid = worker.enqueue_job("bad", ["SP1;", "XX;", "PD1,1;", "PU;", "SP0;"], [1], {1: 10})
    assert wait_for(lambda: status(worker, jid) == "done")
    assert worker.jobs[jid]["warnings"] == 1
    assert worker.jobs[jid]["last_plotter_error"] == "1"


def test_custom_command_roundtrip(worker):
    assert "Not connected" in worker.send_custom_command("PU")
    worker.connect("SIM", 9600)
    assert worker.send_custom_command("PU0,0") == "0"
    assert worker.last_custom_command == "PU0,0;"


def test_query_position_and_limits(worker):
    assert worker.read_position() is None          # not connected
    worker.connect("SIM", 9600)
    worker.send_custom_command("PA;PU1000,2000;")
    assert worker.read_position() == (1000, 2000, False)
    worker.send_custom_command("PR;PD-100,50;PA;")
    assert worker.read_position() == (900, 2050, True)
    assert worker.read_limits() == (0, 0, 16700, 11800)
    ok, reply = worker.query("OE;")
    assert ok and reply == "0"
