"""Check tracked publication candidates, not ignored local runtime state."""
from pathlib import Path
import re
import subprocess
import sys

root = Path(__file__).resolve().parent.parent
files = subprocess.check_output(['git','ls-files','-z'], cwd=root).decode().split('\0')
patterns = [re.compile(rb'/' + rb'Users/[^/\s]+/'),
            re.compile(rb'(?i)(?:gh[pousr]_[A-Za-z0-9]{30,}|-----BEGIN [A-Z ]*PRIVATE KEY-----)'),
            re.compile(rb'[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}')]
errors = []
for name in filter(None, files):
    path = root/name
    if path.is_symlink():
        errors.append(name+': symlink is not a portable publication artifact')
        continue
    if not path.is_file():
        errors.append(name+': tracked file missing')
        continue
    if any(part in ('private','local','.potetos','__pycache__') for part in path.relative_to(root).parts) or path.suffix in ('.sqlite','.db','.log'):
        errors.append(name+': local runtime artifact tracked')
        continue
    content = path.read_bytes()
    for index, pattern in enumerate(patterns):
        for match in pattern.finditer(content):
            if index == 2 and match.group().endswith(b'@example.invalid'):
                continue
            errors.append(name+': private path, credential or non-example identity candidate')
            break
if errors:
    print('\n'.join(errors), file=sys.stderr)
    sys.exit(1)
print(f'PASS: {len(list(filter(None, files)))} tracked files checked; manual diff/history review still required')
