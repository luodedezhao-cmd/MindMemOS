"""Private, append-only, fsynced compression evidence; never used as a usage ledger."""
import json
import os
from pathlib import Path
import stat
import threading
import time


class AuditWriteError(OSError):
    code = 'audit_write_error'


class CompressionAudit:
    def __init__(self, log_dir, instance_id=None, session_id='chat'):
        self.directory = Path(log_dir) / 'audit'
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        if self.directory.is_symlink():
            raise AuditWriteError('Unsafe audit directory')
        self.directory.chmod(0o700)
        self.path = self.directory / 'compression_audit.jsonl'
        self.instance_id = instance_id
        self.session_id = session_id
        self.lock = threading.Lock()
        self.failed = False

    def __call__(self, event):
        with self.lock:
            if self.failed:
                raise AuditWriteError('Audit writer unavailable')
            try:
                record = {**event, 'schema_version': 1, 'time': time.time(),
                          'instance_id': self.instance_id, 'session_id': self.session_id}
                data = (json.dumps(record, ensure_ascii=False, allow_nan=False) + '\n').encode('utf-8')
                fd = os.open(self.path, os.O_APPEND | os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
                try:
                    info = os.fstat(fd)
                    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                        raise AuditWriteError('Unsafe audit file')
                    os.fchmod(fd, 0o600)
                    if info.st_size:
                        os.lseek(fd, -1, os.SEEK_END)
                        if os.read(fd, 1) != b'\n':
                            raise AuditWriteError('Incomplete audit tail')
                    while data:
                        count = os.write(fd, data)
                        if count <= 0:
                            raise AuditWriteError('Incomplete audit write')
                        data = data[count:]
                    os.fsync(fd)
                finally:
                    os.close(fd)
                directory_fd = os.open(self.directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
            except Exception:
                # After a partial write do not append more JSON to a damaged line.
                self.failed = True
                raise AuditWriteError('Compression audit write failed') from None


def persist(audit_sink, event, event_sink=None):
    if audit_sink is None:
        return
    try:
        audit_sink(event)
    except Exception:
        if event_sink is not None:
            try:
                event_sink({'event': 'compression_audit_error', 'call_id': event.get('call_id'),
                            'cause': event.get('event'), 'error_type': 'AuditWriteError'})
            except Exception:
                pass
        raise AuditWriteError('Compression audit write failed') from None
