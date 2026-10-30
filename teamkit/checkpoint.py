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
