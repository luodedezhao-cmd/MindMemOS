"""Interrupt established urllib connections without changing routing/redirect policy."""
from http.client import HTTPConnection, HTTPSConnection
import socket
import threading
from urllib.request import HTTPHandler, HTTPSHandler


class CompressionCancelled(Exception):
    code = 'cancelled'


class Cancellation:
    def __init__(self):
        self._lock = threading.Lock()
        self.cancelled = threading.Event()
        self.request_dispatched = False
        self._sockets = []

    def check(self):
        if self.cancelled.is_set():
            raise CompressionCancelled('compression cancelled')

    @staticmethod
    def _interrupt(sock):
        try:
            sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass

    def register(self, sock):
        if sock is None:
            return
        with self._lock:
            if self.cancelled.is_set():
                self._interrupt(sock)
                self.check()
            self._sockets.append(sock)

    def sending(self):
        # Conservative: once sending can start, no claim of zero provider cost.
        with self._lock:
            self.check()
            self.request_dispatched = True

    def cancel(self):
        with self._lock:
            self.cancelled.set()
            for sock in self._sockets:
                self._interrupt(sock)


def handlers(cancellation):
    class ConnectionMixin:
        def connect(self):
            cancellation.check()
            super().connect()
            cancellation.register(self.sock)

        def send(self, data):
            cancellation.check()
            if self.sock is None:
                self.connect()
            cancellation.sending()
            return super().send(data)

    class HTTP(ConnectionMixin, HTTPConnection):
        pass

    class HTTPS(ConnectionMixin, HTTPSConnection):
        pass

    class CancellableHTTPHandler(HTTPHandler):
        def http_open(self, req):
            cancellation.check()
            return self.do_open(HTTP, req)

    class CancellableHTTPSHandler(HTTPSHandler):
        def https_open(self, req):
            cancellation.check()
            return self.do_open(HTTPS, req, context=self._context,
                                check_hostname=self._check_hostname)

    return CancellableHTTPHandler(), CancellableHTTPSHandler()
