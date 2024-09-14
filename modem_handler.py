import serial
import time
import threading
import argparse
import re

class ModemHandler:
    def __init__(self, port='/dev/ttyUSB0', baudrate=115200, timeout=1):
        self.port = port
        self.baudrate = baudrate
        self.timeout = timeout
        self.ser = None
        self.running = False
        self.lock = threading.Lock()

    def connect(self):
        baudrates = [115200, 9600, 57600, 38400, 19200]
        for baudrate in baudrates:
            try:
                print(f"Attempting to connect with baudrate {baudrate}...")
                self.ser = serial.Serial(self.port, baudrate, timeout=self.timeout)
                self.baudrate = baudrate
                print(f"Connected to {self.port} at {baudrate} baud")
                return True
            except serial.SerialException as e:
                print(f"Failed to connect to {self.port} at {baudrate} baud: {e}")
            except TimeoutError as e:
                print(f"Connection timed out at {baudrate} baud: {e}")
            except Exception as e:
                print(f"Unexpected error at {baudrate} baud: {e}")
        print("Failed to connect at any baudrate")
        return False

    def send_command(self, command, wait_time=1):
        with self.lock:
            if not self.ser or not self.ser.is_open:
                print("Modem is not connected. Attempting to reconnect...")
                if not self.connect():
                    return "Error: Modem not connected"
            try:
                self.ser.write((command + '\r\n').encode())
                time.sleep(wait_time)
                response = ''
                while self.ser.in_waiting:
                    response += self.ser.read(self.ser.in_waiting).decode()
                return response.strip()
            except Exception as e:
                print(f"Error sending command: {e}")
                return f"Error: {str(e)}"

    def listen_for_events(self):
        self.running = True
        while self.running:
            try:
                if not self.ser or not self.ser.is_open:
                    if not self.connect():
                        time.sleep(5)
                        continue
                self.send_command('AT+CMGF=1')  # Set SMS text mode
                self.send_command('AT+CNMI=2,1,0,0,0')  # Configure new message indications
                while self.running and self.ser.is_open:
                    try:
                        response = self.ser.readline().decode().strip()
                        if response:
                            if response.startswith('+CMTI:'):
                                print("Nuevo SMS recibido!")
                                self.handle_new_sms(response)
                            elif response.startswith('RING'):
                                print("Llamada entrante!")
                                self.handle_incoming_call()
                    except serial.SerialException as e:
                        print(f"Serial error: {e}")
                        break
            except Exception as e:
                print(f"Error in event listener: {e}")
                time.sleep(5)
        self.running = True
        self.send_command('AT+CMGF=1')  # Set SMS text mode
        self.send_command('AT+CNMI=2,1,0,0,0')  # Configure new message indications
        
        while self.running:
            response = self.ser.readline().decode().strip()
            if response.startswith('+CMTI:'):
                print("Nuevo SMS recibido!")
                self.handle_new_sms(response)
            elif response.startswith('RING'):
                print("Llamada entrante!")
                self.handle_incoming_call()

    def handle_new_sms(self, notification):
        # Extract the message index
        match = re.search(r'\+CMTI:\s*"[^"]+",\s*(\d+)', notification)
        if match:
            index = match.group(1)
            content = self.send_command(f'AT+CMGR={index}')
            print(f"Contenido del SMS:\n{content}")

    def handle_incoming_call(self):
        # You can add more sophisticated call handling here
        print("Llamada entrante detectada")

    def stop(self):
        self.running = False
        if self.ser and self.ser.is_open:
            self.ser.close()
        self.running = False

    def close(self):
        self.ser.close()

def main():
    parser = argparse.ArgumentParser(description="Manejador de módem para SMS, llamadas y comandos AT")
    parser.add_argument("--port", default="/dev/ttyUSB0", help="Puerto serial (default: /dev/ttyUSB0)")
    parser.add_argument("--baudrate", type=int, default=115200, help="Baudrate (default: 115200)")
    args = parser.parse_args()

    modem = ModemHandler(port=args.port, baudrate=args.baudrate)

    if not modem.connect():
        print("Failed to connect to the modem. Please check the connection and try again.")
        return

    listen_thread = threading.Thread(target=modem.listen_for_events)
    listen_thread.start()

    print("Escuchando eventos del módem. Escribe comandos AT o 'quit' para salir.")

    try:
        while True:
            command = input("Ingrese un comando AT: ")
            if command.lower() == 'quit':
                break
            if '"' in command:
                command = command.replace('"', '\\"')
            response = modem.send_command(command)
            print(f"Respuesta:\n{response}")
    except KeyboardInterrupt:
        print("\nInterrupción detectada. Cerrando...")
    finally:
        modem.stop()
        listen_thread.join()

if __name__ == "__main__":
    main()