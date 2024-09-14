# IoT Modem Manager

## Overview
IoT Modem Manager is a Python-based toolkit designed to interface with 4G modems, specifically tailored for the Dongle USB A7670SA module. This project aims to provide a comprehensive solution for managing SMS, handling calls, and executing AT commands, making it ideal for various IoT applications.

## Features
- Real-time monitoring of incoming SMS messages
- Detection of incoming calls
- Interactive AT command interface
- Support for complex AT commands, including those with quotation marks
- Designed for use with Orange Pi (compatible with other platforms)

## Hardware Requirements
- Orange Pi (or similar single-board computer)
- Dongle USB A7670SA 4G modem
- Active SIM card with data plan

## Software Requirements
- Python 3.x
- pyserial library

## Installation
1. Clone the repository:
   ```
   git clone https://github.com/yourusername/iot-modem-manager.git
   ```
2. Navigate to the project directory:
   ```
   cd iot-modem-manager
   ```
3. Install required dependencies:
   ```
   pip install pyserial
   ```

## Usage
Run the main script:
```
python3 modem_handler.py
```

Optional arguments:
- `--port`: Specify the serial port (default: /dev/ttyUSB1)
- `--baudrate`: Set the baudrate (default: 115200)

Example:
```
python3 modem_handler.py --port /dev/ttyUSB0 --baudrate 9600
```

## AT Command Examples
- Check modem connectivity: `AT`
- Get modem information: `AT+CGMI`
- Check signal strength: `AT+CSQ`
- List all SMS: `AT+CMGL="ALL"`

## Future Development
- Implement MQTT support for IoT data transmission
- Add support for GPS functionality (if supported by the modem)
- Develop a web interface for remote modem management
- Implement data usage tracking and alerts


## Troubleshooting

### Stopping ModemManager

On some systems, ModemManager might automatically try to manage the modem, which can interfere with our script. To prevent this, you may need to stop the ModemManager service:

1. Check if ModemManager is using the modem:
   ```
   sudo lsof /dev/ttyUSB1
   ```

2. If you see ModemManager in the output, stop the service:
   ```
   sudo systemctl stop ModemManager
   ```

3. To prevent ModemManager from starting automatically on boot:
   ```
   sudo systemctl disable ModemManager
   ```

4. If you need ModemManager for other devices, you can create a udev rule to ignore your specific modem. Create a file `/etc/udev/rules.d/99-ignore-modem.rules` with the following content:
   ```
   ACTION=="add|change", ATTRS{idVendor}=="2c7c", ATTRS{idProduct}=="0800", ENV{ID_MM_DEVICE_IGNORE}="1"
   ```
   Replace "2c7c" and "0800" with your modem's vendor and product IDs, which you can find using `lsusb`.

5. Reload udev rules:
   ```
   sudo udevadm control --reload-rules && sudo udevadm trigger
   ```

Note: Stopping ModemManager might affect other cellular devices on your system. Only do this if you're sure you don't need ModemManager for other purposes.


## Contributing
Contributions to the IoT Modem Manager project are welcome. Please feel free to submit pull requests, create issues or suggest new features.

## License
[MIT License](LICENSE)

## Acknowledgements
- Thanks to the pyserial library developers
- Special thanks to the Orange Pi community for their support

## Contact
For any queries or suggestions, please open an issue in the GitHub repository.