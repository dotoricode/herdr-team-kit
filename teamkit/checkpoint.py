"""Fingerprint the worktree without storing source or diffs in the ledger."""
import hashlib
from pathlib import Path
import subprocess


def git_bytes(root, *args):
    return subprocess.run(['git','-C',str(root),*args], capture_output=True, check=True).stdout


def fingerprint(root):
    digest = hashlib.sha256()
    for args in (('status','--porcelain=v1','-z'), ('diff','--binary','HEAD')):
        digest.update(git_bytes(root, *args))
    for name in git_bytes(root,'ls-files','--others','--exclude-standard','-z').split(b'\0'):
        if not name:
            continue
        path = Path(root)/name.decode('utf-8', 'surrogateescape')
        digest.update(name)
        if path.is_symlink():
            digest.update(str(path.readlink()).encode())
        elif path.is_file():
            with path.open('rb') as stream:
                for chunk in iter(lambda: stream.read(65536), b''):
                    digest.update(chunk)
    return digest.hexdigest()


from contextlib import contextmanager
import os


@contextmanager
def revision_guard(root):
    """Hold Git's index, HEAD and current branch locks while recording a review."""
    root = Path(root).resolve()
    storage = subprocess.run(['git','-C',str(root),'config','--get','extensions.refStorage'],
                             capture_output=True, text=True)
    if storage.returncode not in (0, 1):
        raise ValueError('cannot determine Git ref storage')
    if storage.returncode == 0 and storage.stdout.strip().lower() != 'files':
        raise ValueError('review completion supports files ref storage only; use a separate files-format review repository')
    acquired = []

    def lock(git_path):
        raw = git_bytes(root, 'rev-parse', '--git-path', git_path).decode().strip()
        path = Path(raw)
        if not path.is_absolute():
            path = root/path
        path = Path(str(path)+'.lock')
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError as error:
            raise ValueError('Git operation already in progress; retry review completion after it settles') from error
        try:
            acquired.append((path, os.fstat(fd).st_ino))
        finally:
            os.close(fd)

    try:
        lock('index')
        lock('HEAD')
        ref = subprocess.run(['git','-C',str(root),'symbolic-ref','-q','HEAD'], capture_output=True, text=True)
        if ref.returncode == 0:
            lock(ref.stdout.strip())
        elif ref.returncode != 1:
            raise ValueError('cannot determine reviewed HEAD')
        yield
    finally:
        for path, inode in reversed(acquired):
            try:
                if path.stat().st_ino == inode:
                    path.unlink()
            except FileNotFoundError:
                pass
