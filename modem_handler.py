import serial
import time
import threading
import argparse
import re

class ModemHandler:
    def __init__(self, port='/dev/ttyUSB1', baudrate=115200, timeout=1):
        self.ser = serial.Serial(port, baudrate, timeout=timeout)
        self.running = False
        self.lock = threading.Lock()

    def send_command(self, command, wait_time=1):
        with self.lock:
            try:
                self.ser.write((command + '\r\n').encode())
                time.sleep(wait_time)
                response = ''
                while self.ser.in_waiting:
                    response += self.ser.read(self.ser.in_waiting).decode()
                return response.strip()
            except Exception as e:
                return f"Error: {str(e)}"

    def listen_for_events(self):
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

    def close(self):
        self.ser.close()

def main():
    parser = argparse.ArgumentParser(description="Manejador de módem para SMS, llamadas y comandos AT")
    parser.add_argument("--port", default="/dev/ttyUSB1", help="Puerto serial (default: /dev/ttyUSB1)")
    parser.add_argument("--baudrate", type=int, default=115200, help="Baudrate (default: 115200)")
    args = parser.parse_args()

    modem = ModemHandler(port=args.port, baudrate=args.baudrate)

    # Iniciar el hilo de escucha
    listen_thread = threading.Thread(target=modem.listen_for_events)
    listen_thread.start()

    print("Escuchando eventos del módem. Escribe comandos AT o 'quit' para salir.")

    try:
        while True:
            command = input("Ingrese un comando AT: ")
            if command.lower() == 'quit':
                break
            # Manejar comandos con comillas dobles
            if '"' in command:
                command = command.replace('"', '\\"')
            response = modem.send_command(command)
            print(f"Respuesta:\n{response}")
    except KeyboardInterrupt:
        print("\nInterrupción detectada. Cerrando...")
    finally:
        modem.stop()
        listen_thread.join()
        modem.close()

if __name__ == "__main__":
    main()