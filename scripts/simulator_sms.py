"""Local SMS delivery and a serial AT modem for the FactoryVPN simulator.

Only transport is simulated. Messages come from the real API; signature and
expiry checks remain in the production gateway agent. Never log SMS bodies.
"""

from contextlib import contextmanager
import hmac
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import os
from pathlib import Path
import pty
import re
import select
import sqlite3
import threading
import time
import tty

PHONE = "+15550100001"
DATABASE = Path("/sms/messages.sqlite3")


class Inbox:
    """Bounded, persistent queue shared by provider, delivery CLI and modem."""

    def __init__(self, path=DATABASE):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with self.connection() as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS messages ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, phone TEXT NOT NULL, "
                "body TEXT NOT NULL, delivered INTEGER NOT NULL DEFAULT 0, "
                "created REAL NOT NULL, UNIQUE(phone, body))"
            )
        self.path.chmod(0o600)

    @contextmanager
    def connection(self):
        db = sqlite3.connect(self.path, timeout=5)
        try:
            # Serialize capacity checks and delivery with inserts from the API.
            with db:
                db.execute("BEGIN IMMEDIATE")
                yield db
        finally:
            db.close()

    def accept(self, phone, body):
        if phone != PHONE or not isinstance(body, str) or not re.fullmatch(r"[ -~]{1,200}", body):
            raise ValueError("Invalid simulator destination or SMS body")
        with self.connection() as db:
            # This is a demonstration inbox, not an archival log of signed tokens.
            db.execute("DELETE FROM messages WHERE created < ?", (time.time() - 900,))
            if db.execute("SELECT count(*) FROM messages").fetchone()[0] >= 100:
                raise OverflowError("Simulator inbox full")
            db.execute(
                "INSERT OR IGNORE INTO messages(phone, body, created) VALUES (?, ?, ?)",
                (phone, body, time.time()),
            )

    def deliver(self, phone):
        with self.connection() as db:
            return db.execute(
                "UPDATE messages SET delivered=1 WHERE phone=? AND delivered=0", (phone,)
            ).rowcount

    def messages(self, phone):
        with self.connection() as db:
            return db.execute(
                "SELECT id, body FROM messages WHERE phone=? AND delivered=1 ORDER BY id", (phone,)
            ).fetchall()

    def delete(self, phone, identifier):
        with self.connection() as db:
            db.execute("DELETE FROM messages WHERE phone=? AND id=?", (phone, identifier))

    def counts(self, phone):
        with self.connection() as db:
            rows = dict(
                db.execute(
                    "SELECT delivered, count(*) FROM messages WHERE phone=? GROUP BY delivered",
                    (phone,),
                )
            )
        return {"pending": rows.get(0, 0), "delivered": rows.get(1, 0)}


class Modem:
    """Minimal text-mode GSM contract, exposed through a real pseudo-terminal.

    Keep the slave descriptor open until shutdown so startup/restart of pyserial
    does not make the master fail with EIO. Each gateway gets its own PTY.
    """

    def __init__(self, inbox, phone=PHONE, imei="990000000000001"):
        self.inbox, self.phone, self.imei = inbox, phone, imei
        self.master, self.slave = pty.openpty()
        tty.setraw(self.slave)
        self.path = os.ttyname(self.slave)
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self.serve, daemon=True)
        self.thread.start()

    def response(self, command):
        if command in {"AT", "ATE0", "AT+CMGF=1", "AT+CNMI=2,1,0,0,0"}:
            return "\r\nOK\r\n"
        inventory = {
            "AT+CGSN": self.imei,
            "AT+CGMI": "FactoryVPN",
            "AT+CGMM": "Simulated SMS modem",
            "AT+CGMR": "1.0",
        }
        if command in inventory:
            return "\r\n" + inventory[command] + "\r\nOK\r\n"
        if command == 'AT+CMGL="ALL"':
            return (
                "\r\n"
                + "".join(
                    f'+CMGL: {identifier},"REC UNREAD","FactoryVPN","",""\r\n{body}\r\n'
                    for identifier, body in self.inbox.messages(self.phone)
                )
                + "\r\nOK\r\n"
            )
        if re.fullmatch(r"AT\+CMGD=[0-9]+", command):
            self.inbox.delete(self.phone, int(command.split("=")[1]))
            return "\r\nOK\r\n"
        return "\r\nERROR\r\n"

    def serve(self):
        buffer = b""
        while not self.stop.is_set():
            if not select.select([self.master], [], [], 0.1)[0]:
                continue
            buffer += os.read(self.master, 1024)
            while b"\r" in buffer:
                command, buffer = buffer.split(b"\r", 1)
                response = self.response(command.decode("ascii", errors="replace").strip())
                os.write(self.master, response.encode("ascii"))
            if len(buffer) > 1024:
                buffer = b""  # A broken serial client cannot grow memory indefinitely.

    def close(self):
        self.stop.set()
        self.thread.join(timeout=6)
        os.close(self.master)
        os.close(self.slave)


def provider_handler(inbox, key):
    if len(key) < 32:
        raise ValueError("A simulator provider key of at least 32 characters is required")

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass  # Neither Authorization nor SMS contents belong in logs.

        def reply(self, status):
            self.send_response(status)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def do_GET(self):
            self.reply(200 if self.path == "/health" else 404)

        def do_POST(self):
            if self.path != "/send":
                return self.reply(404)
            auth = self.headers.get("Authorization", "").encode()
            if not hmac.compare_digest(auth, ("Bearer " + key).encode()):
                return self.reply(401)
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 1 <= length <= 1024:
                    return self.reply(413)
                self.connection.settimeout(2)
                payload = json.loads(self.rfile.read(length))
                if not isinstance(payload, dict):
                    return self.reply(400)
                inbox.accept(payload.get("to"), payload.get("message"))
            except (ValueError, OSError):
                return self.reply(400)
            except OverflowError:
                return self.reply(503)
            self.reply(202)  # Accepted by provider; NOT yet delivered to the modem.

    return Handler


def main():
    if os.getenv("FACTORYVPN_LOCAL_LAB") != "1" or not Path("/.dockerenv").exists():
        raise SystemExit("Run only inside the FactoryVPN simulator container.")
    os.umask(0o077)
    handler = provider_handler(Inbox(), os.environ["FACTORYVPN_SIMULATOR_SMS_KEY"])
    HTTPServer(("0.0.0.0", 8080), handler).serve_forever()


if __name__ == "__main__":
    main()
