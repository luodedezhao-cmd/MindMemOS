"""Main forwarding: no compressor deadline; close upstream on native cancellation."""
from contextlib import contextmanager
from http.client import HTTPConnection, HTTPSConnection
import select
import socket
import threading
from urllib.parse import urlsplit


@contextmanager
def main_response(url, body, headers, timeout, downstream):
    parsed = urlsplit(url)
    cls = HTTPSConnection if parsed.scheme == 'https' else HTTPConnection
    conn = cls(parsed.hostname, parsed.port, timeout=timeout)
    stop, cancelled = threading.Event(), threading.Event()
    response_socket = [None]

    def disconnected():
        try:
            return bool(select.select([downstream], [], [], 0)[0]) and not downstream.recv(1, socket.MSG_PEEK)
        except OSError:
            return True

    def watch():
        while not stop.wait(.05):
            if disconnected():
                cancelled.set()
                sock = conn.sock or response_socket[0]
                if sock:
                    try:
                        sock.shutdown(socket.SHUT_RDWR)
                    except OSError:
                        pass
                return

    watcher = threading.Thread(target=watch, daemon=True)
    watcher.start()
    try:
        if disconnected():
            raise ConnectionAbortedError('native_client_cancelled')
        conn.connect()
        if cancelled.is_set() or disconnected():
            raise ConnectionAbortedError('native_client_cancelled')
        conn.request('POST', parsed.path + (('?' + parsed.query) if parsed.query else ''), body,
                     {k.title():v for k,v in headers.items()})
        with conn.getresponse() as response:
            response_socket[0] = getattr(getattr(response.fp, 'raw', None), '_sock', None)
            yield response
            if cancelled.is_set():
                raise ConnectionAbortedError('native_client_cancelled')
    finally:
        stop.set()
        conn.close()
        watcher.join(.2)
