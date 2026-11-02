"""Best-effort local wakeups; the durable ledger and periodic scan are authoritative."""
from contextlib import contextmanager
import os
from pathlib import Path
import select
import socket


def address(state):
    import hashlib
    # macOS UNIX socket paths have a small fixed length.
    key = hashlib.sha256(str(Path(state).resolve()).encode()).hexdigest()[:20]
    return str(Path('/tmp') / ('teamkit-wake-' + str(os.getuid()) + '-' + key + '.sock'))


def notify(state):
    with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as sock:
        sock.setblocking(False)
        try:
            sock.sendto(b'changed', address(state))
            return True
        except OSError:
            return False


@contextmanager
def listener(state):
    """Call only while holding the supervisor's exclusive lifetime lock."""
    path = address(state)
    if os.path.lexists(path):
        info = os.lstat(path)
        import stat
        if info.st_uid != os.getuid() or not stat.S_ISSOCK(info.st_mode):
            raise ValueError('wake socket ownership/type mismatch')
        os.unlink(path)
    with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as sock:
        sock.bind(path)
        os.chmod(path, 0o600)
        sock.setblocking(False)
        try:
            yield sock
        finally:
            if os.path.lexists(path):
                os.unlink(path)


def wait(sock, timeout):
    if select.select([sock], [], [], min(timeout, 5))[0]:
        while True:
            try:
                sock.recv(8192)
            except BlockingIOError:
                break
