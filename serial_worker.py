"""
Background worker that owns the serial connection and the job queue.

One daemon thread sends the commands of the current job one at a time, each
followed by ``OE;`` and a wait for the plotter's error-code reply. Jobs can be
queued while disconnected; they wait until a connection exists and the queue
is not on hold.
"""
from __future__ import annotations

import logging
import threading
import time
import uuid

import serial

from plotter_sim import FakeSerial, is_simulated_port

log = logging.getLogger("plotterdash.serial")

FINISHED_STATUSES = ("done", "error", "stopped", "cancelled")

# Seconds to wait between commands after the plotter has acknowledged one.
COMMAND_DELAY = 0.1
# Seconds to wait for the OE; reply. A 50-pair chunk at a slow speed can take
# a long time to draw, so this is generous.
READ_TIMEOUT = 120.0


class SerialWorker:
    def __init__(self, simulate: bool = False, command_delay: float = COMMAND_DELAY,
                 read_timeout: float = READ_TIMEOUT):
        self.simulate = simulate
        self.command_delay = command_delay
        self.read_timeout = read_timeout

        self.serial_conn = None
        self.is_connected = False
        self.port = None
        self.baudrate = None

        self._state = threading.RLock()
        self._cv = threading.Condition(self._state)
        self._serial_lock = threading.Lock()

        self.jobs: dict[str, dict] = {}
        self.queue: list[str] = []          # job ids waiting, in order
        self.current_job_id: str | None = None
        self.queue_held = False

        self.pause_flag = threading.Event()
        self.stop_flag = threading.Event()

        self.last_custom_command = None
        self.last_custom_response = None
        self.last_error = None
        self.last_position: dict | None = None   # most recent OA; reply, in plotter units

        self.worker_thread = threading.Thread(target=self._worker_loop, daemon=True)
        self.worker_thread.start()

    # ------------------------------------------------------------------ connection
    def _open_port(self, port, baudrate):
        if self.simulate and is_simulated_port(port):
            return FakeSerial(port=port, baudrate=baudrate, timeout=self.read_timeout)
        return serial.Serial(
            port=port,
            baudrate=baudrate,
            stopbits=serial.STOPBITS_ONE,
            bytesize=serial.EIGHTBITS,
            parity=serial.PARITY_NONE,
            xonxoff=False,
            rtscts=True,
            dsrdtr=False,
            timeout=self.read_timeout,
            write_timeout=self.read_timeout,
        )

    def connect(self, port: str, baudrate: int) -> tuple[bool, str]:
        """Open the port and verify the plotter answers ``OE;`` with ``0``."""
        with self._serial_lock:
            if self.is_connected:
                return False, f"Already connected to {self.port}"
            try:
                conn = self._open_port(port, baudrate)
            except (serial.SerialException, OSError, ValueError) as e:
                log.warning("Could not open %s: %s", port, e)
                return False, f"Could not open {port}: {e}"

            try:
                conn.timeout = min(5.0, self.read_timeout)
                conn.write(b"OE;")
                response = conn.read_until(b"\r", size=2)
                conn.timeout = self.read_timeout
            except (serial.SerialException, OSError) as e:
                conn.close()
                return False, f"Serial error during handshake: {e}"

            if response.strip() != b"0":
                conn.close()
                log.warning("Unexpected OE reply on %s: %r", port, response)
                if not response:
                    return False, f"No reply from plotter on {port}. Is it switched on and in the right mode?"
                return False, f"Unexpected reply {response!r} from {port}"

            self.serial_conn = conn
            self.port = port
            self.baudrate = baudrate
            self.is_connected = True
            self.last_error = None
            log.info("Connected to %s @ %s", port, baudrate)

        with self._cv:
            self._cv.notify_all()
        return True, f"Connected to {port}"

    def disconnect(self) -> None:
        with self._serial_lock:
            self._close_locked()

    def _close_locked(self) -> None:
        if self.serial_conn is not None:
            try:
                if self.serial_conn.is_open:
                    self.serial_conn.close()
            except (serial.SerialException, OSError):
                pass
        self.serial_conn = None
        self.is_connected = False
        log.info("Port closed.")

    def _transact(self, cmd: str, timeout: float | None = None) -> tuple[bool, str]:
        """
        Send ``cmd`` followed by ``OE;`` and wait for the reply.
        Returns (ok, reply). ``ok`` is False only for transport failures
        (not connected, timeout, serial error); a non-zero plotter error code
        is still ``ok`` with the code in ``reply``.
        """
        with self._serial_lock:
            conn = self.serial_conn
            if not self.is_connected or conn is None or not conn.is_open:
                return False, "Not connected"
            try:
                if timeout is not None:
                    conn.timeout = timeout
                conn.write((cmd + "OE;").encode("utf-8"))
                response = conn.read_until(b"\r", size=2)
            except (serial.SerialException, OSError) as e:
                log.error("Serial error: %s", e)
                self.last_error = f"Serial error: {e}"
                self._close_locked()
                return False, self.last_error
            finally:
                if timeout is not None and conn is not None:
                    try:
                        conn.timeout = self.read_timeout
                    except Exception:
                        pass

        if not response:
            self.last_error = "Plotter did not acknowledge the command (timeout)"
            return False, self.last_error
        return True, response.decode(errors="ignore").strip()

    def query(self, cmd: str, timeout: float = 5.0) -> tuple[bool, str]:
        """
        Send an output command (``OA;``, ``OH;`` ...) and return its reply line.
        Unlike :meth:`_transact` no ``OE;`` is appended, since the reply itself
        is what we want. Returns (ok, reply).
        """
        with self._serial_lock:
            conn = self.serial_conn
            if not self.is_connected or conn is None or not conn.is_open:
                return False, "Not connected"
            try:
                conn.timeout = min(timeout, self.read_timeout)
                try:
                    conn.reset_input_buffer()
                except (AttributeError, OSError):
                    pass
                conn.write(cmd.encode("utf-8"))
                response = conn.read_until(b"\r", size=64)
            except (serial.SerialException, OSError) as e:
                log.error("Serial error: %s", e)
                self.last_error = f"Serial error: {e}"
                self._close_locked()
                return False, self.last_error
            finally:
                if conn is not None:
                    try:
                        conn.timeout = self.read_timeout
                    except Exception:
                        pass
        if not response:
            return False, "Plotter did not reply"
        return True, response.decode(errors="ignore").strip()

    def read_position(self) -> tuple[int, int, bool] | None:
        """Ask the plotter where the pen is (``OA;``). Returns (x, y, pen_down) in plotter units."""
        ok, reply = self.query("OA;")
        if not ok:
            return None
        parts = [p.strip() for p in reply.split(",")]
        try:
            x, y = int(parts[0]), int(parts[1])
            pen_down = bool(int(parts[2])) if len(parts) > 2 and parts[2] else False
        except (ValueError, IndexError):
            log.warning("Unparseable OA reply: %r", reply)
            return None
        self.last_position = {"x": x, "y": y, "pen_down": pen_down}
        return x, y, pen_down

    def read_limits(self) -> tuple[int, int, int, int] | None:
        """Ask the plotter for its hard-clip limits (``OH;``): (x0, y0, x1, y1) in plotter units."""
        ok, reply = self.query("OH;")
        if not ok:
            return None
        try:
            x0, y0, x1, y1 = (int(p.strip()) for p in reply.split(",")[:4])
        except ValueError:
            log.warning("Unparseable OH reply: %r", reply)
            return None
        return x0, y0, x1, y1

    def send_custom_command(self, cmd: str) -> str:
        cmd = cmd.strip()
        if cmd and not cmd.endswith(";"):
            cmd += ";"
        ok, reply = self._transact(cmd, timeout=min(10.0, self.read_timeout))
        self.last_custom_command = cmd
        self.last_custom_response = reply if ok else f"Error: {reply}"
        log.info("Custom %r -> %r", cmd, self.last_custom_response)
        return self.last_custom_response

    # ------------------------------------------------------------------ queue
    def enqueue_job(self, filename: str, commands: list[str], pens: list[int],
                    pen_speeds: dict[int, int], estimated_seconds: float = 0.0,
                    kind: str = "plot", preview_svg: str | None = None) -> str:
        job_id = uuid.uuid4().hex[:12]
        job = {
            "job_id": job_id,
            "filename": filename,
            "kind": kind,
            "commands": commands,
            "current_index": 0,
            "total_commands": len(commands),
            "status": "queued",
            "progress_percent": 0,
            "pens": list(pens),
            "pen_speeds": {int(k): int(v) for k, v in pen_speeds.items()},
            "estimated_seconds": float(estimated_seconds),
            "warnings": 0,
            "last_plotter_error": None,
            "error": None,
            "created_at": time.time(),
            "started_at": None,
            "finished_at": None,
            "preview_svg": preview_svg,
        }
        with self._cv:
            self.jobs[job_id] = job
            self.queue.append(job_id)
            self._cv.notify_all()
        log.info("Enqueued %s (%s, %d commands)", job_id, filename, len(commands))
        return job_id

    def cancel_job(self, job_id: str) -> bool:
        with self._cv:
            job = self.jobs.get(job_id)
            if not job:
                return False
            if job["status"] == "queued":
                self.queue.remove(job_id)
                job["status"] = "cancelled"
                job["finished_at"] = time.time()
                return True
            if job["status"] == "running":
                self.stop_current_job()
                return True
        return False

    def remove_job(self, job_id: str) -> bool:
        with self._cv:
            job = self.jobs.get(job_id)
            if not job or job["status"] == "running":
                return False
            if job_id in self.queue:
                self.queue.remove(job_id)
            del self.jobs[job_id]
            return True

    def rerun_job(self, job_id: str) -> str | None:
        with self._state:
            job = self.jobs.get(job_id)
            if not job:
                return None
            return self.enqueue_job(
                job["filename"], job["commands"], job["pens"], job["pen_speeds"],
                job["estimated_seconds"], job["kind"], job.get("preview_svg"),
            )

    def move_job(self, job_id: str, direction: str) -> bool:
        with self._cv:
            if job_id not in self.queue:
                return False
            i = self.queue.index(job_id)
            j = i - 1 if direction == "up" else i + 1
            if j < 0 or j >= len(self.queue):
                return False
            self.queue[i], self.queue[j] = self.queue[j], self.queue[i]
            return True

    def clear_finished(self) -> int:
        with self._state:
            finished = [jid for jid, j in self.jobs.items() if j["status"] in FINISHED_STATUSES]
            for jid in finished:
                del self.jobs[jid]
            return len(finished)

    def set_queue_held(self, held: bool) -> None:
        with self._cv:
            self.queue_held = bool(held)
            self._cv.notify_all()

    # ------------------------------------------------------------------ job control
    def pause_current_job(self) -> bool:
        if self.current_job_id is None:
            return False
        self.pause_flag.set()
        log.info("Pause requested.")
        return True

    def resume_current_job(self) -> bool:
        if self.current_job_id is None:
            return False
        self.pause_flag.clear()
        log.info("Resume requested.")
        return True

    def stop_current_job(self) -> bool:
        if self.current_job_id is None:
            return False
        self.stop_flag.set()
        log.info("Stop requested.")
        return True

    # ------------------------------------------------------------------ worker
    def _worker_loop(self) -> None:
        while True:
            with self._cv:
                while not (self.queue and self.is_connected and not self.queue_held):
                    self._cv.wait(timeout=0.5)
                job_id = self.queue.pop(0)
                self.current_job_id = job_id
            try:
                self._process_job(job_id)
            except Exception:  # keep the worker alive whatever happens
                log.exception("Unhandled error while processing job %s", job_id)
                job = self.jobs.get(job_id)
                if job:
                    job["status"] = "error"
                    job["error"] = "Internal error, see server log"
                    job["finished_at"] = time.time()
            finally:
                with self._cv:
                    self.current_job_id = None
                self.stop_flag.clear()
                self.pause_flag.clear()

    def _process_job(self, job_id: str) -> None:
        job = self.jobs.get(job_id)
        if not job:
            return

        job["status"] = "running"
        job["progress_percent"] = 0
        job["current_index"] = 0
        job["started_at"] = time.time()
        log.info("Starting job %s (%s)", job_id, job["filename"])

        total = job["total_commands"]
        outcome = "running"
        for i, cmd in enumerate(job["commands"]):
            if self.stop_flag.is_set():
                outcome = "stopped"
                break

            if self.pause_flag.is_set():
                self._transact("PU;")  # lift the pen while waiting
                while self.pause_flag.is_set() and not self.stop_flag.is_set():
                    time.sleep(0.2)
                if self.stop_flag.is_set():
                    outcome = "stopped"
                    break

            ok, reply = self._transact(cmd)
            if not ok:
                outcome = "error"
                job["error"] = reply
                break
            if reply not in ("0", ""):
                job["warnings"] += 1
                job["last_plotter_error"] = reply
                log.warning("Plotter error %s on command %d: %s", reply, i + 1, cmd)

            job["current_index"] = i + 1
            job["progress_percent"] = int((i + 1) / total * 100) if total else 100
            if self.command_delay:
                time.sleep(self.command_delay)

        if outcome == "running":
            outcome = "done" if job["current_index"] == total else "error"

        if outcome == "stopped":
            # Never leave a pen down or out of the carousel after a stop.
            self._transact("PU;")
            self._transact("SP0;")

        job["finished_at"] = time.time()
        job["status"] = outcome
        log.info("Job %s ended with status %s", job_id, outcome)

    # ------------------------------------------------------------------ reporting
    def _public_job(self, job: dict) -> dict:
        now = time.time()
        started = job["started_at"]
        finished = job["finished_at"]
        elapsed = (finished or now) - started if started else 0.0
        return {
            "job_id": job["job_id"],
            "filename": job["filename"],
            "kind": job["kind"],
            "status": job["status"],
            "progress_percent": job["progress_percent"],
            "current_index": job["current_index"],
            "total_commands": job["total_commands"],
            "pens": job["pens"],
            "pen_speeds": {str(k): v for k, v in job["pen_speeds"].items()},
            "estimated_seconds": job["estimated_seconds"],
            "elapsed_seconds": elapsed,
            "warnings": job["warnings"],
            "last_plotter_error": job["last_plotter_error"],
            "error": job["error"],
            "created_at": job["created_at"],
            "has_preview": bool(job.get("preview_svg")),
        }

    def get_status_snapshot(self) -> dict:
        with self._state:
            ordered: list[dict] = []
            if self.current_job_id and self.current_job_id in self.jobs:
                ordered.append(self.jobs[self.current_job_id])
            ordered.extend(self.jobs[jid] for jid in self.queue if jid in self.jobs)
            finished = [j for j in self.jobs.values()
                        if j["status"] in FINISHED_STATUSES]
            finished.sort(key=lambda j: j["finished_at"] or 0, reverse=True)
            ordered.extend(finished)

            return {
                "is_connected": self.is_connected,
                "port": self.port if self.is_connected else None,
                "baudrate": self.baudrate if self.is_connected else None,
                "simulated": bool(self.simulate and is_simulated_port(self.port)) if self.is_connected else False,
                "is_running": self.current_job_id is not None,
                "is_paused": self.pause_flag.is_set(),
                "queue_held": self.queue_held,
                "queued_count": len(self.queue),
                "current_job_id": self.current_job_id,
                "jobs": [self._public_job(j) for j in ordered],
                "last_custom_command": self.last_custom_command,
                "last_custom_response": self.last_custom_response,
                "last_error": self.last_error,
                "position": self.last_position if self.is_connected else None,
            }

    def get_job(self, job_id: str) -> dict | None:
        return self.jobs.get(job_id)
