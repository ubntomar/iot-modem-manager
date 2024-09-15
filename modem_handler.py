import serial
import time
import threading
import argparse
import re
import logging
import glob
import queue
import psutil

logging.basicConfig(level=logging.DEBUG, format='%(asctime)s - %(levelname)s - %(message)s')

# Número de teléfono global para respuestas
RESPONSE_PHONE_NUMBER = "3147654655"

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
        self.processed_messages = {}  # Cambiado a un diccionario

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
            'AT+CSCS="GSM"',  # Set character set
            'AT+CNMI=2,1,0,0,0',  # Configure new message indications
            'AT+CLIP=1',  # Enable Calling Line Identification Presentation
        ]
        for cmd in initialization_commands:
            response = self.send_command(cmd)
            logging.info(f"Initialization command {cmd} response: {response}")

    def find_working_port(self):
        tty_ports = glob.glob('/dev/ttyUSB*')
        for port in tty_ports:
            try:
                self.port = port
                self.ser = serial.Serial(self.port, self.baudrate, timeout=self.timeout)
                response = self.send_command('AT')
                if 'OK' in response:
                    logging.info(f"Found working port: {port}")
                    return True
                self.ser.close()
            except Exception as e:
                logging.debug(f"Failed to connect to {port}: {e}")
        logging.error("No working port found")
        return False

    def send_command(self, command, wait_time=2):
        with self.lock:
            if not self.ser or not self.ser.is_open:
                logging.warning("Modem is not connected. Attempting to reconnect...")
                if not self.connect():
                    return "Error: Modem not connected"
            
            try:
                self.current_command = command
                logging.debug(f"Sending command: {command}")
                self.ser.write((command + '\r\n').encode())
                
                response = self.wait_for_response(wait_time)
                
                logging.debug(f"Raw command response:\n{response}")
                return response
            except Exception as e:
                logging.error(f"Error sending command: {e}")
                return f"Error: {str(e)}"
            finally:
                self.current_command = None

    def wait_for_response(self, timeout):
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
                logging.error(f"Error reading from serial port: {e}")
                break
        return '\n'.join(response)

    def read_serial(self):
        buffer = ""
        while self.running:
            try:
                if self.ser.in_waiting:
                    data = self.ser.read(self.ser.in_waiting).decode(errors='ignore')
                    buffer += data
                    lines = buffer.split('\n')
                    buffer = lines.pop()
                    for line in lines:
                        line = line.strip()
                        if line:
                            logging.debug(f"Raw serial data: {line}")
                            if self.current_command and (line == self.current_command or line in ['OK', 'ERROR', '>'] or line.startswith('+')):
                                self.response_queue.put(line)
                            elif '+CMTI:' in line:
                                logging.info(f"SMS notification received: {line}")
                                self.event_queue.put(line)
                            else:
                                self.event_queue.put(line)
            except serial.SerialException as e:
                logging.error(f"Serial error in read_serial: {e}")
                break
            except Exception as e:
                logging.error(f"Unexpected error in read_serial: {e}")
            time.sleep(0.1)

    def handle_incoming_sms(self, notification):
        match = re.search(r'\+CMTI:\s*"[^"]+",\s*(\d+)', notification)
        if match:
            index = match.group(1)
            content = self.send_command(f'AT+CMGR={index}')
            logging.info(f"Raw SMS content:\n{content}")
            parsed_content = self.parse_sms_content(content)
            logging.info(f"Parsed SMS content:\n{parsed_content}")
            
            if parsed_content and 'message' in parsed_content:
                message_id = (parsed_content['sender'], parsed_content['timestamp'], parsed_content['message'])
                if message_id in self.processed_messages:
                    logging.info(f"Duplicate message detected. Skipping processing.")
                else:
                    self.processed_messages[message_id] = time.time()
                    self.process_sms_command(parsed_content)
            
            # Delete the message after reading
            delete_response = self.send_command(f'AT+CMGD={index}')
            logging.info(f"Delete SMS response: {delete_response}")

            # Limpiar mensajes procesados antiguos
            self.clean_processed_messages()

    def parse_sms_content(self, content):
        lines = content.split('\n')
        if len(lines) < 2:
            logging.error(f"Unexpected SMS format: {content}")
            return None
        
        header = next((line for line in lines if line.startswith('+CMGR:')), '')
        
        if not header:
            logging.error(f"CMGR header not found in content: {content}")
            return None
        
        # Parse header
        header_match = re.search(r'\+CMGR:\s*"([^"]+)",\s*"([^"]+)",(.*?),\s*"([^"]+)"', header)
        if header_match:
            status, sender, _, timestamp = header_match.groups()
        else:
            logging.error(f"Failed to parse header: {header}")
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
        command = sms_data['message'].strip().lower()
        logging.info(f"Processing command: {command}")
        if command == 'cpu':
            cpu_usage = self.get_cpu_usage()
            response = f"CPU Usage: {cpu_usage}%"
        elif command == 'ram':
            ram_info = self.get_ram_info()
            response = f"Available RAM: {ram_info}"
        else:
            response = f"Unknown command: {command}"
            logging.info(f"Unknown command received: {command}")

        # Encolar el SMS de respuesta
        self.outgoing_sms_queue.put((RESPONSE_PHONE_NUMBER, response))

    def get_cpu_usage(self):
        return psutil.cpu_percent(interval=1)

    def get_ram_info(self):
        ram = psutil.virtual_memory()
        return f"{ram.available / (1024 * 1024):.2f} MB"

    def handle_outgoing_sms(self):
        while self.running:
            try:
                phone_number, message = self.outgoing_sms_queue.get(timeout=1)
                self.send_sms(phone_number, message)
            except queue.Empty:
                continue
            except Exception as e:
                logging.error(f"Error handling outgoing SMS: {e}")

    def clean_processed_messages(self):
        current_time = time.time()
        old_messages = [msg_id for msg_id, timestamp in self.processed_messages.items() if current_time - timestamp > 3600]  # 1 hora
        for msg_id in old_messages:
            del self.processed_messages[msg_id]

    def send_sms(self, phone_number, message):
        logging.info(f"Sending SMS to {phone_number}: {message}")
        # Configurar modo texto y codificación
        self.send_command('AT+CMGF=1')
        self.send_command('AT+CSCS="GSM"')
        
        # Enviar comando para iniciar el SMS
        response = self.send_command(f'AT+CMGS="{phone_number}"', wait_time=5)
        
        if '>' in response:
            # Enviar el mensaje y el carácter Ctrl+Z
            full_message = message + chr(26)
            response = self.send_command(full_message, wait_time=10)
            
            if "+CMGS:" in response:
                logging.info(f"SMS enviado exitosamente a {phone_number}")
                return True
            else:
                logging.error(f"Error al enviar SMS. Respuesta: {response}")
                return False
        else:
            logging.error(f"No se recibió el prompt para enviar el SMS. Respuesta: {response}")
            return False

    def listen_for_events(self):
        while self.running:
            try:
                event = self.event_queue.get(timeout=0.5)
                logging.debug(f"Event received: {event}")
                if '+CMTI:' in event:
                    logging.info("New SMS notification received!")
                    self.handle_incoming_sms(event)
                elif event == 'RING':
                    logging.info("Incoming call!")
                    self.handle_incoming_call()
                elif event in ['OK', 'ERROR']:
                    logging.debug(f"Modem response: {event}")
                else:
                    logging.debug(f"Unhandled event: {event}")
            except queue.Empty:
                pass
            except Exception as e:
                logging.error(f"Error in event listener: {e}")

    def handle_incoming_call(self):
        # Implementar lógica para manejar llamadas entrantes si es necesario
        pass

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
    parser.add_argument("--response-number", default="3147654655", help="Phone number to send system info responses")
    args = parser.parse_args()

    logging.getLogger().setLevel(args.log_level)

    global RESPONSE_PHONE_NUMBER
    RESPONSE_PHONE_NUMBER = args.response_number

    modem = ModemHandler(port=args.port, baudrate=args.baudrate)

    if not modem.connect():
        logging.error("Failed to connect to the modem. Please check the connection and try again.")
        return

    listen_thread = threading.Thread(target=modem.listen_for_events)
    listen_thread.start()

    outgoing_sms_thread = threading.Thread(target=modem.handle_outgoing_sms)
    outgoing_sms_thread.start()

    logging.info("Modem handler ready. Type 'send_sms' to send a message, 'at' to enter AT command mode, or 'quit' to exit.")
    logging.info(f"The modem is now listening for incoming SMS messages. System info responses will be sent to {RESPONSE_PHONE_NUMBER}")

    try:
        while True:
            command = input("Enter command: ")
            if command.lower() == 'quit':
                break
            elif command.lower() == 'send_sms':
                phone_number = input("Enter the phone number: ")
                message = input("Enter the message: ")
                modem.outgoing_sms_queue.put((phone_number, message))
                logging.info("SMS queued for sending")
            elif command.lower() == 'at':
                while True:
                    at_command = input("Enter AT command (or 'back' to return): ")
                    if at_command.lower() == 'back':
                        break
                    response = modem.send_command(at_command)
                    logging.info(f"Response:\n{response}")
            else:
                logging.warning("Unknown command. Use 'send_sms', 'at', or 'quit'.")
    except KeyboardInterrupt:
        logging.info("\nInterruption detected. Closing...")
    finally:
        modem.stop()
        listen_thread.join()
        outgoing_sms_thread.join()

if __name__ == "__main__":
    main()