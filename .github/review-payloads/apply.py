"""Temporary transport verifier; never included in the production tree."""
import base64
import gzip
import hashlib
import json
import subprocess
import tempfile
from pathlib import Path

root = Path(__file__).parent
repairs = json.loads((root / 'repairs.json').read_text())
parts = []
for i in range(1, 5):
    text = base64.b64encode((root / f'{i}.patch.gz').read_bytes()).decode()
    for start, end, old, new in reversed(repairs[str(i)]):
        if text[start:end] != old:
            raise ValueError('Transport repair precondition mismatch')
        text = text[:start] + new + text[end:]
    parts.append(gzip.decompress(base64.b64decode(text, validate=True)))
data = b''.join(parts)
expected = '7e4ae55b752d1c61be7e723d64ee2635624ad90ecf9a777d30c160c9bdf334e5'
if hashlib.sha256(data).hexdigest() != expected:
    raise ValueError('Patch differs from locally reviewed/tested source')
with tempfile.NamedTemporaryFile(suffix='.patch') as handle:
    handle.write(data)
    handle.flush()
    subprocess.run(['git', 'apply', '--check', handle.name], check=True)
    subprocess.run(['git', 'apply', handle.name], check=True)
print('Applied exact reviewed patch:', expected)
