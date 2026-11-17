"""Loopback SMTP sink for isolated tests. Never installed in a deployment image."""

from contextlib import contextmanager, asynccontextmanager
from email import policy
from email.parser import BytesParser
import os
import re
import socketserver
import threading
from fastapi import FastAPI, HTTPException
from api.config import settings

messages = {}
test_deliveries = {}


class SMTPHandler(socketserver.StreamRequestHandler):
    quit_reply = b"221 Bye\r\n"

    def handle(self):
        self.connection.settimeout(10)
        self.wfile.write(b"220 FactoryVPN test SMTP\r\n")
        while line := self.rfile.readline(8192):
            command = line.split(b" ", 1)[0].strip().upper()
            if command in (b"EHLO", b"HELO"):
                self.wfile.write(b"250-localhost\r\n250 SIZE 65536\r\n")
            elif command in (b"MAIL", b"RCPT", b"RSET", b"NOOP"):
                self.wfile.write(b"250 OK\r\n")
            elif command == b"DATA":
                self.wfile.write(b"354 Send message\r\n")
                data = bytearray()
                while chunk := self.rfile.readline(8192):
                    if chunk == b".\r\n":
                        break
                    data.extend(chunk[1:] if chunk.startswith(b"..") else chunk)
                    if len(data) > 65536:
                        return
                message = BytesParser(policy=policy.default).parsebytes(bytes(data))
                match = re.search(r"\b\d{6}\b", message.get_content())
                if match:
                    messages[str(message["To"]).lower()] = match.group()
                if str(message["Subject"]) == f"{settings.APP_NAME} — verifica SMTP":
                    recipient = str(message["To"]).lower()
                    test_deliveries[recipient] = test_deliveries.get(recipient, 0) + 1
                self.wfile.write(b"250 Stored\r\n")
            elif command == b"QUIT":
                self.wfile.write(self.quit_reply)
                return
            else:
                self.wfile.write(b"502 Unsupported\r\n")


@contextmanager
def smtp_sink(port=0, host="127.0.0.1"):
    with socketserver.ThreadingTCPServer((host, port), SMTPHandler) as server:
        server.daemon_threads = True
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield server.server_address[1]
        finally:
            server.shutdown()
            thread.join()


@asynccontextmanager
async def lifespan(app):
    if not os.getenv("FACTORYVPN_TEST_DATABASE_URL"):
        raise RuntimeError("Isolated test database required")
    with smtp_sink(
        int(os.environ["SMTP_PORT"]), os.getenv("FACTORYVPN_TEST_SMTP_BIND", "127.0.0.1")
    ):
        yield


app = FastAPI(lifespan=lifespan)


@app.get("/health")
def health():
    return {"ready": True}


@app.get("/messages/{recipient}")
def read_code(recipient: str):
    if recipient not in messages:
        raise HTTPException(404)
    return {"code": messages[recipient]}


@app.get("/test-deliveries/{recipient}")
def read_test_deliveries(recipient: str):
    return {"count": test_deliveries.get(recipient, 0)}
