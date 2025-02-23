import serial
import time

serial_port = "/dev/tty.usbserial-D30IJD40"  # Adjust to your actual port
serial_baud_rate = 9600

stop_bits = serial.STOPBITS_ONE
byte_size = serial.EIGHTBITS
parity = serial.PARITY_NONE

def generate_pd_command(num_pairs):
    """
    Generates a PD command with a given number of (x,y) coordinate pairs.
    We'll just generate coordinates that increment by 10 for both x and y.
    """
    coords = []
    x_start, y_start = 1000, 1000
    for i in range(num_pairs):
        x = x_start + i * 10
        y = y_start + i * 10
        coords.append(f"{x},{y}")
    return "PD" + ",".join(coords) + ";"

try:
    with serial.Serial(
        port=serial_port,
        baudrate=serial_baud_rate,
        stopbits=stop_bits,
        bytesize=byte_size,
        parity=parity,
        xonxoff=False,
        rtscts=True,
        dsrdtr=False,
        timeout=2  # Set a timeout so reads won't block forever
    ) as comx:
        
        # Initialize the plotter
        comx.write("IN;".encode("utf-8"))
        time.sleep(0.5)

        # Start testing from 1 pair and go upwards
        num_pairs = 100
        max_pairs_to_test = 5000  # Adjust to a suitable upper limit

        while num_pairs <= max_pairs_to_test:
            cmd = generate_pd_command(num_pairs)
            print(f"Sending PD with {num_pairs} pairs...")
            comx.write(cmd.encode("utf-8"))
            
            # Give the plotter a moment to process
            time.sleep(0.5)

            # Request error code
            comx.write("OE;".encode("utf-8"))
            time.sleep(0.5)

            # Read response until we hit a ';' or timeout
            response = comx.read_until(b';', size=2)

            # Print response to see what we got
            print(f"Response for {num_pairs} pairs:", response)

            # Check if response indicates an error
            # The plotter might return "0;" for no error, 
            # and some other code (e.g., "2;") if there's a parameter error.
            # Adjust the condition according to your device's documentation.
            if response and response.strip(b';') not in [b'0\r']:  
                print(f"Error encountered at {num_pairs} pairs with response: {response}")
                break

            num_pairs += 1

        else:
            print(f"No error up to {max_pairs_to_test} pairs. You may increase the limit and try again.")

except serial.SerialException as e:
    print("Serial communication error:", e)
