import serial
import time
import threading
import argparse
import re
import logging
import glob
import queue
import psutil
from logging.handlers import RotatingFileHandler

# Configuración del logging con rotación de archivos
log_filename = 'modem_handler.log'
max_log_size = 10 * 1024 * 1024  # Tamaño máximo de cada archivo de log: 10 MB
backup_count = 5  # Número de archivos de backup que se mantendrán

# Configurar el handler de rotación de logs
handler = RotatingFileHandler(log_filename, maxBytes=max_log_size, backupCount=backup_count)
handler.setLevel(logging.DEBUG)
formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
handler.setFormatter(formatter)

# Configurar el logger principal
logger = logging.getLogger()
logger.setLevel(logging.DEBUG)
logger.addHandler(handler)

# También se agrega el StreamHandler para ver los logs en consola
console_handler = logging.StreamHandler()
console_handler.setLevel(logging.INFO)
console_handler.setFormatter(formatter)
logger.addHandler(console_handler)


class ModemHandler:
    def __init__(self, port=None, baudrate=115200, timeout=1):
        self.port = port
        self.baudrate = baudrate
        self.timeout = timeout
        self.ser = None
        self.running = False
        self.lock = threading.Lock()
        self.response_queue = queue.Queue()
        self.event_queue = queue.Queue()
        self.outgoing_sms_queue = queue.Queue()
        self.current_command = None
        self.processed_messages = {}

    def connect(self):
        """Connect to the modem and initialize it. Tries to find a working port if not specified."""
        if not self.port:
            if not self.find_working_port():
                return False

        try:
            self.ser = serial.Serial(self.port, self.baudrate, timeout=self.timeout)
            logger.info(f"Connected to {self.port} at {self.baudrate} baud")
            self.running = True
            self.read_thread = threading.Thread(target=self.read_serial)
            self.read_thread.start()
            self.initialize_modem()
            return True
        except serial.SerialException as e:
            logger.error(f"Failed to connect to serial port: {e}")
            return False
        except Exception as e:
            logger.error(f"Unexpected error during connection: {e}")
            return False

    def initialize_modem(self):
        """Send initialization commands to the modem."""
        initialization_commands = [
            'AT',  # Basic AT command
            'AT+CMGF=1',  # Set SMS text mode
            'AT+CSCS="GSM"',  # Set character set
            'AT+CNMI=2,1,0,0,0',  # Configure new message indications
            'AT+CLIP=1',  # Enable Calling Line Identification Presentation
        ]
        for cmd in initialization_commands:
            response = self.send_command(cmd)
            logger.info(f"Initialization command {cmd} response: {response}")

    def find_working_port(self):
        """Find a working serial port among available ttyUSB ports."""
        tty_ports = glob.glob('/dev/ttyUSB*')
        for port in tty_ports:
            try:
                self.port = port
                self.ser = serial.Serial(self.port, self.baudrate, timeout=self.timeout)
                response = self.send_command('AT')
                if 'OK' in response:
                    logger.info(f"Found working port: {port}")
                    return True
                self.ser.close()
            except Exception as e:
                logger.debug(f"Failed to connect to {port}: {e}")
        logger.error("No working port found")
        return False

    def send_command(self, command, wait_time=2):
        """Send a command to the modem and wait for a response."""
        with self.lock:
            if not self.ser or not self.ser.is_open:
                logger.warning("Modem is not connected. Attempting to reconnect...")
                if not self.connect():
                    return "Error: Modem not connected"
            
            try:
                self.current_command = command
                logger.debug(f"Sending command: {command}")
                self.ser.write((command + '\r\n').encode())
                
                response = self.wait_for_response(wait_time)
                
                logger.debug(f"Raw command response:\n{response}")
                return response
            except Exception as e:
                logger.error(f"Error sending command: {e}")
                return f"Error: {str(e)}"
            finally:
                self.current_command = None

    def wait_for_response(self, timeout):
        """Wait for a response from the modem for a specified timeout period."""
        start_time = time.time()
        response = []
        while time.time() - start_time < timeout:
            try:
                line = self.ser.readline().decode(errors='ignore').strip()
                if line:
                    response.append(line)
                    if line in ['OK', 'ERROR', '>', '+CMS ERROR:'] or '+CMGS:' in line:
                        break
            except serial.SerialException as e:
                logger.error(f"Error reading from serial port: {e}")
                self.running = False  # Stop running on serial error
                break
        return '\n'.join(response)

    def read_serial(self):
        """Continuously read data from the modem and handle it."""
        buffer = ""
        while self.running:
            try:
                if self.ser and self.ser.in_waiting:
                    data = self.ser.read(self.ser.in_waiting).decode(errors='ignore')
                    buffer += data
                    lines = buffer.split('\n')
                    buffer = lines.pop()
                    for line in lines:
                        line = line.strip()
                        if line:
                            logger.debug(f"Raw serial data: {line}")
                            if self.current_command and (line == self.current_command or line in ['OK', 'ERROR', '>'] or line.startswith('+')):
                                self.response_queue.put(line)
                            elif '+CMTI:' in line:
                                logger.info(f"SMS notification received: {line}")
                                self.event_queue.put(line)
                            else:
                                self.event_queue.put(line)
            except serial.SerialException as e:
                logger.error(f"Serial error in read_serial: {e}")
                self.running = False
                break
            except Exception as e:
                logger.error(f"Unexpected error in read_serial: {e}")
                time.sleep(1)  # Delay to avoid spamming errors
            time.sleep(0.1)

    def handle_incoming_sms(self, notification):
        """Handle incoming SMS based on notifications from the modem."""
        match = re.search(r'\+CMTI:\s*"[^"]+",\s*(\d+)', notification)
        if match:
            index = match.group(1)
            content = self.send_command(f'AT+CMGR={index}')
            logger.info(f"Raw SMS content:\n{content}")
            parsed_content = self.parse_sms_content(content)
            logger.info(f"Parsed SMS content:\n{parsed_content}")
            
            if parsed_content and 'message' in parsed_content:
                message_id = (parsed_content['sender'], parsed_content['timestamp'], parsed_content['message'])
                if message_id in self.processed_messages:
                    logger.info(f"Duplicate message detected. Skipping processing.")
                else:
                    self.processed_messages[message_id] = time.time()
                    self.process_sms_command(parsed_content)
            
            # Delete the message after reading
            delete_response = self.send_command(f'AT+CMGD={index}')
            logger.info(f"Delete SMS response: {delete_response}")

            # Clean old processed messages
            self.clean_processed_messages()

    def parse_sms_content(self, content):
        """Parse the content of an SMS received from the modem."""
        lines = content.split('\n')
        if len(lines) < 2:
            logger.error(f"Unexpected SMS format: {content}")
            return None
        
        header = next((line for line in lines if line.startswith('+CMGR:')), '')
        
        if not header:
            logger.error(f"CMGR header not found in content: {content}")
            return None
        
        # Parse header
        header_match = re.search(r'\+CMGR:\s*"([^"]+)",\s*"([^"]+)",(.*?),\s*"([^"]+)"', header)
        if header_match:
            status, sender, _, timestamp = header_match.groups()
        else:
            logger.error(f"Failed to parse header: {header}")
            status, sender, timestamp = "Unknown", "Unknown", "Unknown"
        
        # The message content is in the lines after the header
        message = '\n'.join(line.strip() for line in lines[lines.index(header)+1:] if line.strip() and line.strip() != 'OK')
        
        return {
            "status": status,
            "sender": sender,
            "timestamp": timestamp,
            "message": message
        }

    def process_sms_command(self, sms_data):
        """Process SMS commands received from the modem."""
        command = sms_data['message'].strip().lower()
        sender = sms_data['sender']
        logger.info(f"Processing command: {command} from sender: {sender}")
        
        if command == 'cpu':
            cpu_usage = self.get_cpu_usage()
            response = f"CPU Usage: {cpu_usage}%"
        elif command == 'ram':
            ram_info = self.get_ram_info()
            response = f"Available RAM: {ram_info}"
        elif command == 'signal':
            signal_strength = self.get_signal_strength()
            response = f"Signal Strength: {signal_strength}"
        else:
            response = f"Unknown command: {command}"
            logger.info(f"Unknown command received: {command}")

        # Enqueue the response SMS using the sender's number
        self.outgoing_sms_queue.put((sender, response))

    def get_cpu_usage(self):
        """Get the current CPU usage percentage."""
        return psutil.cpu_percent(interval=1)

    def get_ram_info(self):
        """Get the available RAM in MB."""
        ram = psutil.virtual_memory()
        return f"{ram.available / (1024 * 1024):.2f} MB"

    def handle_outgoing_sms(self):
        """Handle outgoing SMS messages by sending them from the queue."""
        while self.running:
            try:
                phone_number, message = self.outgoing_sms_queue.get(timeout=1)
                self.send_sms(phone_number, message)
            except queue.Empty:
                continue
            except Exception as e:
                logger.error(f"Error handling outgoing SMS: {e}")

    def clean_processed_messages(self):
        """Clean up old processed messages to prevent memory bloat."""
        current_time = time.time()
        old_messages = [msg_id for msg_id, timestamp in self.processed_messages.items() if current_time - timestamp > 3600]  # 1 hour
        for msg_id in old_messages:
            del self.processed_messages[msg_id]

    def send_sms(self, phone_number, message):
        """Send an SMS message using the modem."""
        logger.info(f"Sending SMS to {phone_number}: {message}")
        self.send_command('AT+CMGF=1')  # Set text mode
        self.send_command('AT+CSCS="GSM"')  # Set character set
        
        response = self.send_command(f'AT+CMGS="{phone_number}"', wait_time=5)
        
        if '>' in response:
            full_message = message + chr(26)  # Ctrl+Z to send the message
            response = self.send_command(full_message, wait_time=10)
            
            if "+CMGS:" in response:
                logger.info(f"SMS sent successfully to {phone_number}")
                return True
            else:
                logger.error(f"Error sending SMS. Response: {response}")
                return False
        else:
            logger.error(f"No prompt received to send SMS. Response: {response}")
            return False

    def listen_for_events(self):
        """Listen for events from the modem, such as SMS or incoming calls."""
        while self.running:
            try:
                event = self.event_queue.get(timeout=0.5)
                logger.debug(f"Event received: {event}")
                if '+CMTI:' in event:
                    logger.info("New SMS notification received!")
                    self.handle_incoming_sms(event)
                elif event == 'RING':
                    logger.info("Incoming call!")
                    self.handle_incoming_call()
                elif event in ['OK', 'ERROR']:
                    logger.debug(f"Modem response: {event}")
                else:
                    logger.debug(f"Unhandled event: {event}")
            except queue.Empty:
                pass
            except Exception as e:
                logger.error(f"Error in event listener: {e}")

    def handle_incoming_call(self):
        """Handle incoming calls if needed."""
        pass

    def stop(self):
        """Stop the modem handler and clean up resources."""
        self.running = False
        if self.ser and self.ser.is_open:
            self.ser.close()
        if hasattr(self, 'read_thread'):
            self.read_thread.join()


    def interpret_signal_strength(self, csq_response):
        """Interpreta la respuesta del comando AT+CSQ para obtener la intensidad de la señal."""
        match = re.search(r'\+CSQ:\s*(\d+),', csq_response)
        if match:
            rssi = int(match.group(1))
            if rssi == 99:
                return "No signal"
            elif rssi >= 20:
                return f"Excellent (-{73 - (rssi - 20) * 2} dBm)"
            elif rssi >= 15:
                return f"Good (-{93 - (rssi - 15) * 2} dBm)"
            elif rssi >= 10:
                return f"Fair (-{103 - (rssi - 10) * 2} dBm)"
            else:
                return f"Poor (-{113 - rssi * 2} dBm)"
        return "Unable to determine signal strength"

    def get_signal_strength(self):
        """Obtiene y devuelve la intensidad de la señal interpretada."""
        response = self.send_command('AT+CSQ')
        interpretation = self.interpret_signal_strength(response)
        logger.info(f"Raw signal strength response: {response}")
        return interpretation

def main():
    parser = argparse.ArgumentParser(description="Modem handler for SMS, calls, and AT commands")
    parser.add_argument("--port", help="Serial port (e.g., /dev/ttyUSB0). If not specified, will auto-detect.")
    parser.add_argument("--baudrate", type=int, default=115200, help="Baudrate (default: 115200)")
    parser.add_argument("--log-level", default="DEBUG", choices=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
                        help="Set the logging level")
    parser.add_argument("--response-number", default="3147654655", help="Phone number to send system info responses")
    args = parser.parse_args()

    logger.setLevel(args.log_level)


    modem = ModemHandler(port=args.port, baudrate=args.baudrate)

    if not modem.connect():
        logger.error("Failed to connect to the modem. Please check the connection and try again.")
        return

    listen_thread = threading.Thread(target=modem.listen_for_events)
    listen_thread.start()

    outgoing_sms_thread = threading.Thread(target=modem.handle_outgoing_sms)
    outgoing_sms_thread.start()

    logger.info("Modem handler ready. Type 'send_sms' to send a message, 'at' to enter AT command mode, 'signal'  or 'quit' to exit.")
    logger.info(f"The modem is now listening for incoming SMS messages. Responses will be sent to the sender's number")

    try:
        while True:
            command = input("Enter command: ")
            if command.lower() == 'quit':
                break
            elif command.lower() == 'send_sms':
                phone_number = input("Enter the phone number: ")
                message = input("Enter the message: ")
                modem.outgoing_sms_queue.put((phone_number, message))
                logger.info("SMS queued for sending")
            elif command.lower() == 'at':
                while True:
                    at_command = input("Enter AT command (or 'back' to return): ")
                    if at_command.lower() == 'back':
                        break
                    response = modem.send_command(at_command)
                    logger.info(f"Response:\n{response}")
            elif command.lower() == 'signal':  # Nuevo comando para obtener la intensidad de la señal
                signal_strength = modem.get_signal_strength()
                logger.info(f"Current signal strength: {signal_strength}")
            else:
                logger.warning("Unknown command. Use 'send_sms', 'at', 'signal', or 'quit'.")
    except KeyboardInterrupt:
        logger.info("\nInterruption detected. Closing...")
    finally:
        modem.stop()
        listen_thread.join()
        outgoing_sms_thread.join()

if __name__ == "__main__":
    main()
