from . import Connection
import settings
import socket
from exception import ConnectionLostException
import logging

class ConnectionTCP(Connection):
    def _connect(self):
        hostname = settings.getb(b'CONNECTION_TCP_HOSTNAME')
        port = int(settings.getb(b'CONNECTION_TCP_PORT'))
        self.__sock = socket.create_connection((hostname, port))
        # Drain any stale bytes left in the Lantronix's serial-recv buffer
        # by a previous session. Without this, the first read returns
        # leftover-bytes + partial-real-response, breaking CRC validation.
        self.__sock.settimeout(0.4)
        drained = 0
        try:
            while True:
                d = self.__sock.recv(256)
                if not d: break
                drained += len(d)
        except socket.timeout:
            pass
        self.__sock.settimeout(None)  # back to blocking
        if drained:
            logging.info(f"selpi: drained {drained} stale bytes from Lantronix")

    def _read(self, length: int):
        return self.__sock.recv(length)

    def _write(self, data: bytes):
        attempts = 0
        while attempts < 3:
            try:
                return self.__sock.send(data)
            except BrokenPipeError:
                logging.debug("BrokenPipeError, retrying connection")
                self._connect()
                attempts = attempts + 1
        raise ConnectionLostException("Too many sequential write failures (%s)" % attempts)
