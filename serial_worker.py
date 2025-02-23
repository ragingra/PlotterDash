import serial
import time
import queue  # For FIFO job queue
import threading
import uuid


# -------------------------------------------------------------------
# SERIAL WORKER to send commands & track state
# -------------------------------------------------------------------
class SerialWorker:
    def __init__(self):
        self.serial_conn = None
        self.is_connected = False
        self.port = None
        self.baudrate = None

        # A single background thread that processes jobs one by one
        self.worker_thread = None

        # A queue of job IDs
        self.job_queue = queue.Queue()

        # Dict of job info: {job_id: { ... job details ... }}
        self.jobs_info = {}

        # Flags for controlling the currently running job
        self.pause_flag = threading.Event()
        self.stop_flag = threading.Event()

        self.last_custom_command = None
        self.last_custom_response = None

        # True when the worker loop is actively checking the queue
        self.worker_running = False

        # Start the worker thread
        self._start_worker_thread()

    # -------------------------------------------
    # Connection Management
    # -------------------------------------------
    def connect(self, port, baudrate, stopbits=serial.STOPBITS_ONE, bytesize=serial.EIGHTBITS,
                parity=serial.PARITY_NONE, rtscts=True, timeout=None):
        """
        Open the serial port, test with 'OE;' to check for 0; response.
        """
        try:
            self.serial_conn = serial.Serial(
                port=port,
                baudrate=baudrate,
                stopbits=stopbits,
                bytesize=bytesize,
                parity=parity,
                xonxoff=False,
                rtscts=rtscts,
                dsrdtr=False,
                timeout=timeout
            )
            self.port = port
            self.baudrate = baudrate
            print(f"[SerialWorker] Connected to {port} @ {baudrate} baud.")

            # Test the connection by sending OE;
            test_cmd = "OE;"
            self.serial_conn.write(test_cmd.encode("utf-8"))
            response = self.serial_conn.read_until(b';', size=2)
            print(f"[SerialWorker] OE response: {response}")

            if response !=  b'0\r':
                print("[SerialWorker] Unexpected response on OE;. Might be an error or wrong port.")
                self.is_connected = False
                self.serial_conn.close()
                self.serial_conn = None
            else:
                self.is_connected = True

        except serial.SerialException as e:
            print(f"[SerialWorker] Could not open port: {e}")
            self.is_connected = False
            self.serial_conn = None

    def disconnect(self):
        if self.serial_conn and self.serial_conn.is_open:
            self.serial_conn.close()
            print("[SerialWorker] Port closed.")
        self.serial_conn = None
        self.is_connected = False

    def send_custom_command(self, cmd):
        """
        Send a single HPGL command immediately (outside of the queued jobs).
        We'll append 'OE;' as a terminator to read for an error code.
        """
        if not self.is_connected or not self.serial_conn or not self.serial_conn.is_open:
            print("[SerialWorker] Not connected, cannot send custom command.")
            self.last_custom_command = cmd
            self.last_custom_response = "Not connected."
            return "Not connected."

        full_cmd = cmd + "OE;"
        print(f"[SerialWorker] Sending custom command: {full_cmd}")
        try:
            self.serial_conn.write(full_cmd.encode("utf-8"))
            response = self.serial_conn.read_until(b';', size=2)
            print(f"[SerialWorker] Custom command response: {response}")
            # Store for SSE
            self.last_custom_command = cmd
            self.last_custom_response = response.decode(errors="ignore").strip()

            return self.last_custom_response
        except serial.SerialException as e:
            print("[SerialWorker] Serial exception in send_custom_command:", e)
            self.last_custom_command = cmd
            self.last_custom_response = f"Error: {str(e)}"
            return self.last_custom_response

    # -------------------------------------------
    # Job Management
    # -------------------------------------------
    def enqueue_job(self, filename, commands, pen_speed, pen_number):
        """
        Create a job record and add it to the queue.
        """
        job_id = str(uuid.uuid4())
        job_info = {
            "job_id": job_id,
            "filename": filename,
            "commands": commands,   # list of HPGL commands
            "current_index": 0,
            "total_commands": len(commands),
            "status": "queued",    # "queued" | "running" | "done" | "error" | "stopped"
            "progress_percent": 0,
            "pen_speed": pen_speed,
            "pen_number": pen_number
        }
        self.jobs_info[job_id] = job_info
        self.job_queue.put(job_id)
        print(f"[SerialWorker] Enqueued job {job_id} ({filename}).")

    def pause_current_job(self):
        self.pause_flag.set()
        print("[SerialWorker] Pause requested.")

    def resume_current_job(self):
        self.pause_flag.clear()
        print("[SerialWorker] Resume requested.")

    def stop_current_job(self):
        # This will signal the worker to stop the current job
        self.stop_flag.set()
        print("[SerialWorker] Stop requested.")

    def _start_worker_thread(self):
        """
        Start a daemon thread that continuously checks the queue for new jobs.
        """
        if not self.worker_thread:
            self.worker_thread = threading.Thread(target=self._worker_loop, daemon=True)
            self.worker_thread.start()

    def _worker_loop(self):
        """
        Continuously process jobs from the queue, one at a time.
        """
        self.worker_running = True
        while True:
            job_id = self.job_queue.get()  # blocks until a job is available
            self._process_job(job_id)
            self.job_queue.task_done()

    def _process_job(self, job_id):
        """
        Process a single job by sending commands one by one, respecting pause and stop.
        """
        job = self.jobs_info.get(job_id)
        if not job:
            return

        job["status"] = "running"
        job["progress_percent"] = 0
        job["current_index"] = 0
        print(f"[SerialWorker] Starting job {job_id} ({job['filename']}).")

        try:
            for i, cmd in enumerate(job["commands"]):
                # Check if stop was requested
                if self.stop_flag.is_set():
                    print(f"[SerialWorker] Job {job_id} was stopped.")
                    job["status"] = "stopped"
                    break

                # Check if paused
                while self.pause_flag.is_set():
                    time.sleep(1)

                # Send the command
                if self.is_connected and self.serial_conn and self.serial_conn.is_open:
                    cmd_to_send = cmd + "OE;"
                    print(f"[SerialWorker] Sending [{i+1}/{job['total_commands']}]: {cmd_to_send}")
                    self.serial_conn.write(cmd_to_send.encode("utf-8"))

                    response = self.serial_conn.read_until(b';', size=2)
                    print(f"[SerialWorker] Response: {response}")
                    # Optionally check for error
                else:
                    print("[SerialWorker] Not connected. Cannot send commands.")
                    job["status"] = "error"
                    break

                job["current_index"] = i + 1
                job["progress_percent"] = int((i+1)/job["total_commands"]*100)
                time.sleep(0.1)  # small delay

            # If we finished all commands without stop or error
            if job["status"] == "running" and job["current_index"] == job["total_commands"]:
                job["status"] = "done"

        finally:
            # Reset flags for the next job
            self.stop_flag.clear()
            self.pause_flag.clear()

        print(f"[SerialWorker] Job {job_id} ended with status: {job['status']}.")

    # -------------------------------------------
    # Reporting
    # -------------------------------------------
    def get_status_snapshot(self):
        any_running = any(j["status"] == "running" for j in self.jobs_info.values())
        is_paused = self.pause_flag.is_set()

        jobs_dict = {
            job_id: {
                "job_id": info["job_id"],
                "filename": info["filename"],
                "current_index": info["current_index"],
                "total_commands": info["total_commands"],
                "status": info["status"],
                "progress_percent": info["progress_percent"],
                "pen_speed": info["pen_speed"],
                "pen_number": info["pen_number"]
            }
            for job_id, info in self.jobs_info.items()
        }

        return {
            "is_connected": self.is_connected,
            "is_running": any_running,
            "is_paused": is_paused,
            "jobs": jobs_dict,
            "last_custom_command": self.last_custom_command,
            "last_custom_response": self.last_custom_response
        }


# ----------------------------------------------------------------
# Example usage (CLI or script)
if __name__ == "__main__":
    from hpgl_utils import process_hpgl_commands

    # Prompt user (or supply via command line) for port and baud:
    port_input = input("Enter serial port (e.g., /dev/tty.usbserial-D30IJD40): ")
    baud_input = input("Enter baud rate (e.g., 9600): ")
    
    # Create the worker
    worker = SerialWorker()
    
    # Attempt to connect
    success = worker.connect(
        port=port_input.strip(),
        baudrate=int(baud_input.strip()),
        # Adjust rtscts or other flags if needed
    )
    if not success:
        print("Error: Could not establish a proper connection (OE; check failed). Exiting.")
        exit(1)
    
    # If that works, proceed to load commands from an HPGL file
    hpgl_file = input("Enter path to HPGL file (e.g., /Users/dv-5/Desktop/4.hpgl): ")
    try:
        with open(hpgl_file, "r") as f:
            hpgl_data = f.read()
    except Exception as e:
        print(f"Error reading file: {e}")
        worker.disconnect()
        exit(1)
    
    # parse the HPGL into chunks
    commands_list = process_hpgl_commands(hpgl_data, max_pairs=50)
    worker.load_commands(commands_list)
    
    # Start sending in background
    worker.start()
    
    # Quick demonstration: monitor progress, then pause/resume
    for i in range(5):
        time.sleep(1)
        print(f"Progress: {worker.get_progress():.1f}%")
    
    print("Pausing...")
    worker.pause()
    time.sleep(3)
    print("Resuming...")
    worker.resume()
    
    # Wait until finished
    while worker.is_running:
        print(f"Progress: {worker.get_progress():.1f}%")
        time.sleep(1)
    
    print("All done. Closing port.")
    worker.disconnect()
