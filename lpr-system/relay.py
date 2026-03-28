import serial
import time
import logging
import os

logger = logging.getLogger(__name__)

class RelayController:
    def __init__(self, port=None, baudrate=9600, simulate=False):
        """
        USB 繼電器控制器
        port: COM3 (Windows) 或 /dev/ttyUSB0 (Linux)
        simulate: True = 模擬模式（無硬體時使用）
        """
        self.port = port
        self.baudrate = baudrate
        self.serial = None
        self.simulate = simulate
        if not simulate:
            self._auto_detect()

    def _auto_detect(self):
        """自動偵測 USB 繼電器連接的 COM port"""
        if self.port:
            return
        possible_ports = []
        if os.name == 'nt':  # Windows
            for i in range(1, 21):
                possible_ports.append(f'COM{i}')
        else:  # Linux/Mac
            possible_ports = ['/dev/ttyUSB0', '/dev/ttyUSB1', '/dev/ttyACM0', '/dev/cu.usbserial']

        for p in possible_ports:
            try:
                s = serial.Serial(p, self.baudrate, timeout=1)
                s.close()
                self.port = p
                logger.info(f'自動偵測到繼電器: {p}')
                return
            except:
                continue
        logger.warning('無法自動偵測繼電器，請手動指定 port')

    def _send_command(self, cmd):
        """發送繼電器指令（支援不同品牌的繼電器）"""
        commands = [
            cmd,  # 標準指令
            cmd + b'\r\n',  # 帶換行
            cmd + b'\n',   # 帶換行
        ]
        for c in commands:
            try:
                self.serial.write(c)
                time.sleep(0.3)
                return True
            except:
                continue
        return False

    def connect(self):
        if not self.port:
            return False
        try:
            self.serial = serial.Serial(self.port, self.baudrate, timeout=1)
            time.sleep(0.5)
            return True
        except Exception as e:
            logger.error(f'連接繼電器失敗: {e}')
            return False

    def open_gate(self, duration=1.5):
        """
        觸發繼電器開門
        duration: 開門信號持續時間（秒）
        """
        if self.simulate:
            logger.info(f'[模擬模式] 開門信號已發送，持續 {duration} 秒')
            return True
        if not self.serial or not self.serial.is_open:
            if not self.connect():
                logger.error('無法連接繼電器')
                return False
        try:
            self.serial.write(b'{\xff\x01\x01}')
            time.sleep(duration)
            self.serial.write(b'{\xff\x01\x00}')
            logger.info(f'開門信號已發送，持續 {duration} 秒')
            return True
        except Exception as e:
            logger.error(f'開門失敗: {e}')
            return False

    def close(self):
        if self.serial and self.serial.is_open:
            self.serial.close()

class ModbusTCPController:
    """Modbus TCP/IP 乙太網路繼電器"""
    def __init__(self, ip=None, port=502, coil=0, simulate=False):
        self.ip = ip
        self.port = port
        self.coil = coil  # Coil 位址（預設 0）
        self.simulate = simulate
        self.client = None

    def connect(self):
        if not self.ip:
            return False
        try:
            from pymodbus.client import ModbusTcpClient
            self.client = ModbusTcpClient(self.ip, port=self.port, timeout=5)
            return self.client.connect()
        except Exception as e:
            logger.error(f'Modbus TCP 連線失敗: {e}')
            return False

    def open_gate(self, duration=1.5):
        """發送 Modbus 開門信號"""
        if self.simulate:
            logger.info(f'[模擬模式] Modbus 開門信號已發送，{self.ip}:{self.port} coil={self.coil}，持續 {duration} 秒')
            return True
        if not self.client or not self.client.connected:
            if not self.connect():
                logger.error('Modbus TCP 無法連線')
                return False
        try:
            # 閉合線圈（開門）
            self.client.write_coil(self.coil, True)
            logger.info(f'Modbus 線圈 {self.coil} 閉合')
            time.sleep(duration)
            # 斷開線圈（關門）
            self.client.write_coil(self.coil, False)
            logger.info(f'Modbus 線圈 {self.coil} 斷開，開門完成')
            return True
        except Exception as e:
            logger.error(f'Modbus 開門失敗: {e}')
            return False

    def close(self):
        if self.client and self.client.connected:
            self.client.close()
