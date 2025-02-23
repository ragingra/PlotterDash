import serial

from hpgl_utils import process_hpgl_commands

serial_port = "/dev/tty.usbserial-D30IJD40" 
serial_baud_rate = 9600

stop_bits = serial.STOPBITS_ONE
byte_size = serial.EIGHTBITS
parity = serial.PARITY_NONE

hpgl_input = ""

# open file and read string to variable
with open("/Users/dv-5/Downloads/output.hpgl", "r") as file:
    hpgl_input = file.read()

commands = process_hpgl_commands(hpgl_input, max_pairs=50)

try:
    with serial.Serial(
        port=serial_port,
        baudrate=serial_baud_rate,
        stopbits=stop_bits,
        bytesize=byte_size,
        parity=parity,
        xonxoff=False,  
        rtscts=True,    
        dsrdtr=False
    ) as comx:
        for cmd in commands:
            to_send = cmd + "OE;"
            print("Sending command:", to_send)
            comx.write(to_send.encode("utf-8"))
            
            # Give device time to process the command
            # time.sleep(5)
            
            response = comx.read_until(b';', size=2)

            # Print response to see what we got
            print(f"Response:", response)

            # Check if response indicates an error
            # The plotter might return "0;" for no error, 
            # and some other code (e.g., "2;") if there's a parameter error.
            # Adjust the condition according to your device's documentation.
            if response and response.strip(b';') not in [b'0\r']:  
                print("Error response received!")
                break
        
except serial.SerialException as e:
    print("Serial communication error:", e)
