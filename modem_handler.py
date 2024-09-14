import serial
import time
import threading
import argparse
import re
import logging
import glob
import queue
import binascii

logging.basicConfig(level=logging.DEBUG, format='%(asctime)s - %(levelname)s - %(message)s')

class ModemHandler:
    def __init__(self, port=None, baudrate=115200, timeout=1):
        self.port = port
        self.baudrate = baudrate
        self.timeout = timeout
        self.ser = None
        self.running = False
        self.lock = threading.Lock()
        self.encodings = ['utf-8', 'ascii', 'latin-1', 'iso-8859-1']
        self.response_queue = queue.Queue()
        self.event_queue = queue.Queue()
        self.current_command = None
        self.processed_messages = set()
        self.last_cmti_timestamp = 0

    def find_working_port(self):
        tty_ports = glob.glob('/dev/ttyUSB*')
        baudrates = [115200, 9600, 57600, 38400, 19200]
        
        for port in tty_ports:
            for baudrate in baudrates:
                try:
                    logging.info(f"Attempting to connect to {port} with baudrate {baudrate}...")
                    with serial.Serial(port, baudrate, timeout=self.timeout) as ser:
                        ser.write(b'AT\r\n')
                        time.sleep(1)
                        response = ser.read(ser.in_waiting)
                        if b'OK' in response:
                            logging.info(f"Found working port: {port} at {baudrate} baud")
                            self.port = port
                            self.baudrate = baudrate
                            return True
                except Exception as e:
                    logging.debug(f"Failed to connect to {port} at {baudrate} baud: {e}")
        
        logging.error("No working port found")
        return False

    def connect(self):
        if not self.port:
            if not self.find_working_port():
                return False

        try:
            self.ser = serial.Serial(self.port, self.baudrate, timeout=self.timeout)
            logging.info(f"Connected to {self.port} at {self.baudrate} baud")
            self.running = True
            self.read_thread = threading.Thread(target=self.read_serial)
            self.read_thread.start()
            self.initialize_modem()
            return True
        except Exception as e:
            logging.error(f"Failed to connect: {e}")
            return False

    def initialize_modem(self):
        initialization_commands = [
            'AT',  # Basic AT command
            'AT+CMGF=1',  # Set SMS text mode
            'AT+CPMS="ME","ME","ME"',  # Set memory storage
            'AT+CNMI=2,1,0,0,0',  # Configure new message indications
            'AT+CLIP=1',  # Enable Calling Line Identification Presentation
            'AT+CMGD=1,4',  # Delete all SMS messages
        ]
        for cmd in initialization_commands:
            response = self.send_command(cmd)
            logging.info(f"Initialization command {cmd} response: {response}")
            if "ERROR" in response:
                logging.error(f"Failed to execute command: {cmd}")
                if cmd == 'AT+CPMS="ME","ME","ME"':
                    # Try with different memory types
                    alt_memory_types = ['"SM","SM","SM"', '"MT","MT","MT"']
                    for alt_mem in alt_memory_types:
                        alt_cmd = f'AT+CPMS={alt_mem}'
                        alt_response = self.send_command(alt_cmd)
                        logging.info(f"Trying alternative memory command {alt_cmd} response: {alt_response}")
                        if "OK" in alt_response:
                            logging.info(f"Successfully set memory to {alt_mem}")
                            break
                    else:
                        logging.error("Failed to set any memory type")

        # Check current SMS mode
        mode_response = self.send_command('AT+CMGF?')
        logging.info(f"Current SMS mode: {mode_response}")
        if "1" not in mode_response:
            logging.warning("SMS text mode not set. Attempting to set it again.")
            self.send_command('AT+CMGF=1')

        # Check current memory settings
        memory_response = self.send_command('AT+CPMS?')
        logging.info(f"Current memory settings: {memory_response}")

    def send_command(self, command, wait_time=2, max_attempts=3):
        with self.lock:
            if not self.ser or not self.ser.is_open:
                logging.warning("Modem is not connected. Attempting to reconnect...")
                if not self.connect():
                    return "Error: Modem not connected"
            
            for attempt in range(max_attempts):
                try:
                    self.current_command = command
                    logging.debug(f"Sending command: {command}")
                    self.ser.write((command + '\r\n').encode())
                    
                    response = self.wait_for_response(wait_time)
                    
                    if response:
                        logging.debug(f"Raw command response:\n{response}")
                        return response
                    elif attempt < max_attempts - 1:
                        logging.warning(f"No response received. Retrying... (Attempt {attempt + 1})")
                    else:
                        logging.error("No response received after multiple attempts")
                        return "Error: No response from modem"
                except Exception as e:
                    logging.error(f"Error sending command: {e}")
                    if attempt == max_attempts - 1:
                        return f"Error: {str(e)}"
                finally:
                    self.current_command = None
            
            return "Error: Failed to get response after multiple attempts"

    def wait_for_response(self, timeout):
        start_time = time.time()
        response = []
        while time.time() - start_time < timeout:
            try:
                line = self.response_queue.get(timeout=0.1)
                response.append(line)
                if line in ['OK', 'ERROR']:
                    return '\n'.join(response)
            except queue.Empty:
                pass
        return '\n'.join(response) if response else None

    def read_serial(self):
        buffer = ""
        while self.running:
            if self.ser.in_waiting:
                data = self.ser.read(self.ser.in_waiting).decode(errors='replace')
                buffer += data
                lines = buffer.split('\r\n')
                buffer = lines.pop()
                
                for line in lines:
                    line = line.strip()
                    if line:
                        if self.current_command and (line == self.current_command or line in ['OK', 'ERROR'] or line.startswith('+')):
                            self.response_queue.put(line)
                        else:
                            self.event_queue.put(line)
            time.sleep(0.1)

    def listen_for_events(self):
        while self.running:
            try:
                event = self.event_queue.get(timeout=0.5)
                if '+CMTI:' in event:
                    logging.info("New SMS received!")
                    self.handle_new_sms(event)
                elif event == 'RING':
                    logging.info("Incoming call!")
                    self.handle_incoming_call()
                else:
                    logging.debug(f"Received event: {event}")
            except queue.Empty:
                pass
            except Exception as e:
                logging.error(f"Error in event listener: {e}")
        while self.running:
            try:
                event = self.event_queue.get(timeout=0.5)
                if '+CMTI:' in event:
                    logging.info("New SMS received!")
                    self.handle_new_sms(event)
                elif event == 'RING':
                    logging.info("Incoming call!")
                    self.handle_incoming_call()
                else:
                    logging.debug(f"Received event: {event}")
            except queue.Empty:
                pass
            except Exception as e:
                logging.error(f"Error in event listener: {e}")
        while self.running:
            try:
                event = self.event_queue.get(timeout=0.5)
                if '+CMTI:' in event:
                    logging.info("New SMS received!")
                    self.handle_new_sms(event)
                elif event == 'RING':
                    logging.info("Incoming call!")
                    self.handle_incoming_call()
                else:
                    logging.debug(f"Received event: {event}")
            except queue.Empty:
                pass
            except Exception as e:
                logging.error(f"Error in event listener: {e}")
        while self.running:
            try:
                event = self.event_queue.get(timeout=0.5)
                if '+CMTI:' in event:
                    logging.info("New SMS received!")
                    self.handle_new_sms(event)
                elif event == 'RING':
                    logging.info("Incoming call!")
                    self.handle_incoming_call()
                else:
                    logging.debug(f"Received event: {event}")
            except queue.Empty:
                pass
            except Exception as e:
                logging.error(f"Error in event listener: {e}")
        while self.running:
            try:
                event = self.event_queue.get(timeout=0.5)
                if '+CMTI:' in event:
                    logging.info("New SMS received!")
                    self.handle_new_sms(event)
                elif event == 'RING':
                    logging.info("Incoming call!")
                    self.handle_incoming_call()
                else:
                    logging.debug(f"Received event: {event}")
            except queue.Empty:
                pass
            except Exception as e:
                logging.error(f"Error in event listener: {e}")

    def handle_new_sms(self, notification):
        current_time = time.time()
        if current_time - self.last_cmti_timestamp < 2:
            logging.info("Ignoring duplicate CMTI notification")
            return
        self.last_cmti_timestamp = current_time

        match = re.search(r'\+CMTI:\s*"[^"]+",\s*(\d+)', notification)
        if match:
            index = match.group(1)
            if index in self.processed_messages:
                logging.info(f"Message {index} already processed. Skipping.")
                return

            self.processed_messages.add(index)
            content = self.send_command(f'AT+CMGR={index}')
            logging.info(f"Raw SMS content:\n{content}")
            
            if "OK" not in content:
                logging.error(f"Failed to read message {index}")
                return

            parsed_content = self.parse_sms_content(content)
            
            # Wait for the actual message content
            try:
                additional_content = self.event_queue.get(timeout=2)
                decoded_content = self.decode_hex_string(additional_content)
                parsed_content += f"\nMessage: {decoded_content}"
            except queue.Empty:
                logging.warning("No additional content received for SMS")
            
            logging.info(f"Parsed SMS content:\n{parsed_content}")
            
            # Delete the message after reading
            delete_response = self.send_command(f'AT+CMGD={index}')
            logging.info(f"Delete SMS response: {delete_response}")

            if "OK" not in delete_response:
                logging.error(f"Failed to delete message {index}")
        match = re.search(r'\+CMTI:\s*"[^"]+",\s*(\d+)', notification)
        if match:
            index = match.group(1)
            if index in self.processed_messages:
                logging.info(f"Message {index} already processed. Skipping.")
                return

            self.processed_messages.add(index)
            content = self.send_command(f'AT+CMGR={index}')
            logging.info(f"Raw SMS content:\n{content}")
            parsed_content = self.parse_sms_content(content)
            
            # Wait for the actual message content
            try:
                additional_content = self.event_queue.get(timeout=2)
                decoded_content = self.decode_hex_string(additional_content)
                parsed_content += f"\nMessage: {decoded_content}"
            except queue.Empty:
                logging.warning("No additional content received for SMS")
            
            logging.info(f"Parsed SMS content:\n{parsed_content}")
            
            # Delete the message after reading
            delete_response = self.send_command(f'AT+CMGD={index}')
            logging.info(f"Delete SMS response: {delete_response}")
        match = re.search(r'\+CMTI:\s*"[^"]+",\s*(\d+)', notification)
        if match:
            index = match.group(1)
            content = self.send_command(f'AT+CMGR={index}')
            logging.info(f"Raw SMS content:\n{content}")
            parsed_content = self.parse_sms_content(content)
            
            # Wait for the actual message content
            try:
                additional_content = self.event_queue.get(timeout=2)
                decoded_content = self.decode_hex_string(additional_content)
                parsed_content += f"\nMessage: {decoded_content}"
            except queue.Empty:
                logging.warning("No additional content received for SMS")
            
            logging.info(f"Parsed SMS content:\n{parsed_content}")
            
            # Delete the message after reading
            delete_response = self.send_command(f'AT+CMGD={index}')
            logging.info(f"Delete SMS response: {delete_response}")
        match = re.search(r'\+CMTI:\s*"[^"]+",\s*(\d+)', notification)
        if match:
            index = match.group(1)
            content = self.send_command(f'AT+CMGR={index}')
            logging.info(f"Raw SMS content:\n{content}")
            parsed_content = self.parse_sms_content(content)
            
            # Wait for the actual message content
            try:
                additional_content = self.event_queue.get(timeout=2)
                decoded_content = self.decode_hex_string(additional_content)
                parsed_content += f"\nMessage: {decoded_content}"
            except queue.Empty:
                logging.warning("No additional content received for SMS")
            
            logging.info(f"Parsed SMS content:\n{parsed_content}")
            
            # Delete the message after reading
            delete_response = self.send_command(f'AT+CMGD={index}')
            logging.info(f"Delete SMS response: {delete_response}")
        match = re.search(r'\+CMTI:\s*"[^"]+",\s*(\d+)', notification)
        if match:
            index = match.group(1)
            content = self.send_command(f'AT+CMGR={index}')
            logging.info(f"Raw SMS content:\n{content}")
            parsed_content = self.parse_sms_content(content)
            logging.info(f"Parsed SMS content:\n{parsed_content}")
            
            # Check for additional content in the event queue
            try:
                additional_content = self.event_queue.get(block=False)
                decoded_content = self.decode_hex_string(additional_content)
                logging.info(f"Additional decoded content: {decoded_content}")
            except queue.Empty:
                pass
            
            # Delete the message after reading
            delete_response = self.send_command(f'AT+CMGD={index}')
            logging.info(f"Delete SMS response: {delete_response}")
        match = re.search(r'\+CMTI:\s*"[^"]+",\s*(\d+)', notification)
        if match:
            index = match.group(1)
            content = self.send_command(f'AT+CMGR={index}')
            logging.info(f"Raw SMS content:\n{content}")
            parsed_content = self.parse_sms_content(content)
            logging.info(f"Parsed SMS content:\n{parsed_content}")
            
            # Delete the message after reading
            delete_response = self.send_command(f'AT+CMGD={index}')
            logging.info(f"Delete SMS response: {delete_response}")

    def parse_sms_content(self, content):
        lines = content.split('\n')
        header = next((line for line in lines if line.startswith('+CMGR:')), '')
        
        if not header:
            return "Error: CMGR header not found"
        
        # Parse header
        header_match = re.search(r'\+CMGR: (.+)$', header)
        if header_match:
            header_content = header_match.group(1)
            parts = header_content.split(',')
            if len(parts) >= 4:
                status = parts[0].strip('"')
                sender = self.decode_hex_string(parts[1].strip('"'))
                timestamp = parts[3].strip('"')
            else:
                status, sender, timestamp = "Unknown", "Unknown", "Unknown"
        else:
            status, sender, timestamp = "Unknown", "Unknown", "Unknown"
        
        return f"Status: {status}\nFrom: {sender}\nTimestamp: {timestamp}\nMessage: [Waiting for content]"
        lines = content.split('\n')
        if len(lines) < 2:
            return "Error: Unexpected SMS format"
        
        header = next((line for line in lines if line.startswith('+CMGR:')), '')
        
        if not header:
            return "Error: CMGR header not found"
        
        # Parse header
        header_match = re.search(r'\+CMGR: (.+)$', header)
        if header_match:
            header_content = header_match.group(1)
            parts = header_content.split(',')
            if len(parts) >= 4:
                status = parts[0].strip('"')
                sender = self.decode_hex_string(parts[1].strip('"'))
                timestamp = parts[3].strip('"')
            else:
                status, sender, timestamp = "Unknown", "Unknown", "Unknown"
        else:
            status, sender, timestamp = "Unknown", "Unknown", "Unknown"
        
        return f"Status: {status}\nFrom: {sender}\nTimestamp: {timestamp}\nMessage: [Waiting for content]"
        lines = content.split('\n')
        if len(lines) < 2:
            return "Error: Unexpected SMS format"
        
        header = next((line for line in lines if line.startswith('+CMGR:')), '')
        
        if not header:
            return "Error: CMGR header not found"
        
        # Parse header
        header_match = re.search(r'\+CMGR: (.+)$', header)
        if header_match:
            header_content = header_match.group(1)
            parts = header_content.split(',')
            if len(parts) >= 4:
                status = parts[0].strip('"')
                sender = self.decode_hex_string(parts[1].strip('"'))
                timestamp = parts[3].strip('"')
            else:
                status, sender, timestamp = "Unknown", "Unknown", "Unknown"
        else:
            status, sender, timestamp = "Unknown", "Unknown", "Unknown"
        
        return f"Status: {status}\nFrom: {sender}\nTimestamp: {timestamp}\nMessage: [Waiting for content]"
        lines = content.split('\n')
        if len(lines) < 2:
            return "Error: Unexpected SMS format"
        
        header = lines[1]  # The actual header is in the second line
        
        # Parse header
        header_match = re.search(r'\+CMGR: (.+)$', header)
        if header_match:
            header_content = header_match.group(1)
            parts = header_content.split(',')
            if len(parts) >= 4:
                status = parts[0].strip('"')
                sender = self.decode_hex_string(parts[1].strip('"'))
                timestamp = parts[3].strip('"')
            else:
                status, sender, timestamp = "Unknown", "Unknown", "Unknown"
        else:
            status, sender, timestamp = "Unknown", "Unknown", "Unknown"
        
        # The actual message content is not in this response, it's in the additional content
        return f"Status: {status}\nFrom: {sender}\nTimestamp: {timestamp}\nMessage: [Waiting for content]"
        lines = content.split('\n')
        if len(lines) < 2:
            return "Error: Unexpected SMS format"
        
        header = lines[0]
        message = '\n'.join(lines[1:-1])  # Excluding the last line which is usually just "OK"
        
        # Parse header
        header_match = re.search(r'\+CMGR: (.+)$', header)
        if header_match:
            header_content = header_match.group(1)
            parts = header_content.split(',')
            if len(parts) >= 4:
                status = parts[0].strip('"')
                sender = self.decode_hex_string(parts[1].strip('"'))
                timestamp = parts[3].strip('"')
            else:
                status, sender, timestamp = "Unknown", "Unknown", "Unknown"
        else:
            status, sender, timestamp = "Unknown", "Unknown", "Unknown"
        
        # Decode the message content
        decoded_message = self.decode_hex_string(message)
        
        return f"Status: {status}\nFrom: {sender}\nTimestamp: {timestamp}\nMessage:\n{decoded_message}"
        lines = content.split('\n')
        if len(lines) < 2:
            return "Error: Unexpected SMS format"
        
        header = lines[0]
        message = '\n'.join(lines[1:-1])  # Excluding the last line which is usually just "OK"
        
        # Parse header
        header_match = re.search(r'\+CMGR: (.+)$', header)
        if header_match:
            header_content = header_match.group(1)
            parts = header_content.split(',')
            if len(parts) >= 3:
                status = parts[0].strip('"')
                sender = parts[1].strip('"')
                timestamp = ','.join(parts[2:]).strip('"')  # Join the rest as timestamp (might contain commas)
            else:
                status, sender, timestamp = "Unknown", "Unknown", "Unknown"
        else:
            status, sender, timestamp = "Unknown", "Unknown", "Unknown"
        
        return f"Status: {status}\nFrom: {sender}\nTimestamp: {timestamp}\nMessage:\n{message}"

    def decode_hex_string(self, hex_string):
        try:
            # Remove any non-hex characters
            hex_string = ''.join(c for c in hex_string if c in '0123456789ABCDEFabcdef')
            # Decode the hex string
            byte_string = binascii.unhexlify(hex_string)
            # Try to decode as UTF-16-BE (which is what the format seems to be)
            return byte_string.decode('utf-16-be')
        except Exception as e:
            logging.error(f"Error decoding hex string: {e}")
            return hex_string  # Return the original string if decoding fails
        try:
            # Remove any non-hex characters
            hex_string = ''.join(c for c in hex_string if c in '0123456789ABCDEFabcdef')
            # Decode the hex string
            byte_string = binascii.unhexlify(hex_string)
            # Try to decode as UTF-16-BE (which is what the format seems to be)
            return byte_string.decode('utf-16-be')
        except Exception as e:
            logging.error(f"Error decoding hex string: {e}")
            return hex_string  # Return the original string if decoding fails

    def handle_incoming_call(self):
        caller_id = self.send_command('AT+CLCC')
        logging.info(f"Incoming call detected. Caller ID info:\n{caller_id}")

    def stop(self):
        self.running = False
        if self.ser and self.ser.is_open:
            self.ser.close()
        if hasattr(self, 'read_thread'):
            self.read_thread.join()

def main():
    parser = argparse.ArgumentParser(description="Modem handler for SMS, calls, and AT commands")
    parser.add_argument("--port", help="Serial port (e.g., /dev/ttyUSB0). If not specified, will auto-detect.")
    parser.add_argument("--baudrate", type=int, default=115200, help="Baudrate (default: 115200)")
    parser.add_argument("--log-level", default="DEBUG", choices=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
                        help="Set the logging level")
    args = parser.parse_args()

    logging.getLogger().setLevel(args.log_level)

    modem = ModemHandler(port=args.port, baudrate=args.baudrate)

    if not modem.connect():
        logging.error("Failed to connect to the modem. Please check the connection and try again.")
        return

    # Ejecutar un comando AT de prueba
    test_command = "AT"
    logging.info(f"Executing test command: {test_command}")
    test_response = modem.send_command(test_command)
    logging.info(f"Test command response:\n{test_response}")

    listen_thread = threading.Thread(target=modem.listen_for_events)
    listen_thread.start()

    logging.info("Listening for modem events. Type AT commands or 'quit' to exit.")

    try:
        while True:
            command = input("Enter an AT command: ")
            if command.lower() == 'quit':
                break
            if '"' in command:
                command = command.replace('"', '\\"')
            response = modem.send_command(command)
            logging.info(f"Response:\n{response}")
    except KeyboardInterrupt:
        logging.info("\nInterruption detected. Closing...")
    finally:
        modem.stop()
        listen_thread.join()

if __name__ == "__main__":
    main()