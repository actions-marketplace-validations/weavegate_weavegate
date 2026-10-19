#!/usr/bin/env python3
"""Recompute the committed results from checksummed original evidence."""
from pathlib import Path, PurePosixPath
import subprocess
import sys
import tarfile
import tempfile

from pilot import analyze


source = Path(__file__).resolve().parent
results = source / 'results'
with tempfile.TemporaryDirectory(prefix='weavegate-pilot-analysis-') as name:
    temporary = Path(name)
    for bundle in ('stopped-r1', 'local-r2'):
        suffix = '.tar.gz' if bundle == 'stopped-r1' else '.tar'
        with tarfile.open(results / (bundle + suffix), 'r:*') as archive:
            for member in archive.getmembers():
                relative = PurePosixPath(member.name)
                if (relative.is_absolute() or '..' in relative.parts or not relative.parts or
                        relative.parts[0] != bundle or not (member.isfile() or member.isdir())):
                    raise ValueError('non-regular or unsafe evidence archive entry')
                path = temporary.joinpath(*relative.parts)
                if member.isdir():
                    path.mkdir(parents=True, exist_ok=True)
                else:
                    path.parent.mkdir(parents=True, exist_ok=True)
                    with path.open('xb') as stream:
                        stream.write(archive.extractfile(member).read())
        generated = temporary / (bundle + '.json')
        analyze(temporary / bundle, generated)
        if generated.read_bytes() != (results / (bundle + '.json')).read_bytes():
            raise ValueError('derived JSON differs: ' + bundle)
    rendered = temporary / 'local-r2.md'
    subprocess.run([sys.executable, '-B', str(source / 'render.py'),
                    str(temporary / 'local-r2.json'), str(rendered)], check=True)
    if rendered.read_bytes() != (results / 'local-r2.md').read_bytes():
        raise ValueError('rendered tables differ')
print('LOCAL_PILOT_EVIDENCE_RESULT bundles=2 analysis=reproduced raw=checksummed')
