"""GSM text-mode SMS commands with bounded reads and explicit acknowledgements."""

import csv
import logging
import re
import threading
import time
from dataclasses import dataclass

import serial

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SMS:
    id: int
    sender: str
    body: str
    timestamp: str


class SMSHandler:
    def __init__(self, modem_path="/dev/ttyUSB0", baudrate=9600, timeout=5.0, modem=None):
        self.modem = modem or serial.Serial(modem_path, baudrate=baudrate, timeout=0.2)
        self.timeout = timeout
        self._lock = threading.RLock()
        self._control_pending = threading.Event()
        self._init_modem()

    def _read_response(self, prompt=False):
        deadline = time.monotonic() + self.timeout
        data = bytearray()
        while time.monotonic() < deadline:
            data.extend(self.modem.read(max(1, self.modem.in_waiting)))
            response = data.decode("utf-8", errors="replace")
            # A CMGL body is untrusted message text, even when it consists of
            # "ERROR" or "+CMS ERROR". Only a failure before the first message
            # is a command rejection; otherwise wait for the list's final OK.
            listing = re.search(r"(?:^|\r?\n)\+CMGL:", response)
            if not listing and re.search(r"(?:^|\r?\n)(?:ERROR|\+CM[ES] ERROR:.*)\r?\n", response):
                raise RuntimeError("Modem rejected the command")
            if prompt and response.rstrip().endswith(">"):
                return response
            if response.endswith("\r\nOK\r\n") or response == "OK\r\n":
                return response
        raise TimeoutError("Timed out waiting for modem acknowledgement")

    def _command(self, command, *, inventory=False):
        if not inventory:
            self._control_pending.set()
        try:
            with self._lock:
                self.modem.reset_input_buffer()
                self.modem.write((command + "\r").encode("ascii"))
                self.modem.flush()
                return self._read_response()
        finally:
            if not inventory:
                self._control_pending.clear()

    def _init_modem(self):
        for command in ("AT", "ATE0", "AT+CMGF=1", "AT+CNMI=2,1,0,0,0"):
            self._command(command)

    def list_sms(self):
        # Storage indexes are slots, not monotonic message IDs. A message stays
        # eligible until processing succeeds and delete_sms acknowledges it.
        return self._parse_sms_list(self._command('AT+CMGL="ALL"'))

    def inventory(self, can_probe=lambda: True):
        """One bounded read-only probe, only scheduled outside activation handling."""
        result = {}
        for command, field in (
            ("AT+CGSN", "imei"),
            ("AT+CGMI", "manufacturer"),
            ("AT+CGMM", "model"),
            ("AT+CGMR", "version"),
            ("AT+CCID", "iccid"),
            ("AT+QCCID", "iccid"),
        ):
            if field in result:
                continue
            if not can_probe() or self._control_pending.is_set():
                break
            if not self._lock.acquire(blocking=False):
                break
            previous_timeout = self.timeout
            try:
                self.timeout = min(previous_timeout, 0.4)
                try:
                    response = self._command(command, inventory=True)
                except (RuntimeError, TimeoutError):
                    if field == "imei":
                        raise
                    continue
                lines = [
                    line.strip()
                    for line in response.splitlines()
                    if line.strip() and line.strip() not in {command, "OK"}
                ]
                if field == "iccid":
                    # Varianti di lettura SIM; una risposta non supportata resta facoltativa.
                    match = (
                        re.fullmatch(r'(?:\+(?:Q)?CCID:\s*)?"?([0-9]{18,22})"?', lines[0])
                        if len(lines) == 1
                        else None
                    )
                    if match:
                        result[field] = match.group(1)
                    continue
                pattern = r"[0-9]{15}" if field == "imei" else r"[\w .()+,:@-]{1,128}"
                if len(lines) != 1 or not re.fullmatch(pattern, lines[0]):
                    if field == "imei":
                        raise ValueError("Unsupported modem identifier")
                    continue
                result[field] = lines[0]
            finally:
                self.timeout = previous_timeout
                self._lock.release()
        if not result:
            raise TimeoutError("Modem inventory deferred")
        return result

    def check_new_sms(self):
        messages = self.list_sms()
        return messages[0] if messages else None

    @staticmethod
    def _parse_sms_list(response):
        headers = list(re.finditer(r"(?m)^\+CMGL:\s*([^\r\n]+)\r?\n", response))
        messages = []
        for index, header in enumerate(headers):
            try:
                fields = next(csv.reader([header.group(1)], skipinitialspace=True))
                if len(fields) < 4 or not fields[0].isdigit():
                    continue
                end = headers[index + 1].start() if index + 1 < len(headers) else len(response)
                body = response[header.end() : end]
                if index + 1 == len(headers):
                    body = re.sub(r"\r?\nOK\r?\n\s*$", "", body)
                messages.append(
                    SMS(
                        id=int(fields[0]),
                        sender=fields[2],
                        timestamp=fields[-1],
                        body=body.strip("\r\n").replace("\r\n", "\n"),
                    )
                )
            except (ValueError, csv.Error):
                logger.warning("Ignoring malformed SMS header")
        return messages

    def delete_sms(self, sms_id):
        if not isinstance(sms_id, int) or sms_id < 0:
            raise ValueError("Invalid SMS storage index")
        self._command(f"AT+CMGD={sms_id}")

    def send_sms(self, phone_number, message):
        if not re.fullmatch(r"\+?[1-9]\d{9,14}", phone_number):
            raise ValueError("Invalid phone number")
        if "\x1a" in message or "\x1b" in message:
            raise ValueError("Invalid SMS control character")
        with self._lock:
            self.modem.reset_input_buffer()
            self.modem.write(f'AT+CMGS="{phone_number}"\r'.encode("ascii"))
            self.modem.flush()
            self._read_response(prompt=True)
            self.modem.write(message.encode("utf-8") + b"\x1a")
            self.modem.flush()
            return self._read_response()

    def close(self):
        with self._lock:
            self.modem.close()
