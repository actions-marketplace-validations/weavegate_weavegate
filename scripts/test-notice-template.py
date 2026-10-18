#!/usr/bin/env python3
"""Check NOTICE source normalization using the actual Go text template."""
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
INSTALL_STUB = '''import os, sys
from pathlib import Path
if sys.argv[1] == 'install':
    assert sys.argv[2:] == ['github.com/google/go-licenses@v1.6.0']
    assert os.environ['GOSUMDB'] == 'sum.golang.org'
    assert os.environ['GONOSUMDB'] == ''
    log = Path(os.environ['INSTALL_LOG'])
    attempts = log.read_text() if log.exists() else ''
    log.write_text(attempts + 'install\\n')
    if len(attempts.splitlines()) < int(os.environ['FAIL_INSTALLS']):
        print('sum.golang.org: stream error: INTERNAL_ERROR', file=sys.stderr)
        sys.exit(23)
    binary = Path(os.environ['GOBIN']) / 'go-licenses'
    binary.parent.mkdir(parents=True)
    binary.write_bytes(Path(os.environ['LICENSE_STUB']).read_bytes())
    binary.chmod(0o755)
elif sys.argv[1:] != ['list', 'std']:
    sys.exit('unexpected Go invocation')
'''
REPORT_STUB = '''import os
print('example.invalid/module\\tv1.0.0\\tMIT\\thttps://example.invalid/license\\t' + os.environ['LICENSE_TEXT'])
'''
GO = '''package main
import (
    "encoding/json"
    "os"
    "text/template"
)
type license struct { Name, Version, LicenseName, LicenseURL, LicensePath string }
func main() {
    var entries []license
    if err := json.NewDecoder(os.Stdin).Decode(&entries); err != nil { panic(err) }
    t, err := template.ParseFiles(os.Args[1])
    if err != nil { panic(err) }
    if err := t.Execute(os.Stdout, entries); err != nil { panic(err) }
}
'''


def main():
    yaml = {'Name': 'gopkg.in/yaml.v3', 'Version': 'v3.0.1', 'LicenseName': 'MIT',
            'LicenseURL': 'https://github.com/go-yaml/yaml/blob/v3.0.1/LICENSE',
            'LicensePath': '/module-cache/yaml/LICENSE'}
    entries = [dict(yaml, LicenseURL=url) for url in
               ('Unknown', yaml['LicenseURL'], 'Unknown', yaml['LicenseURL'])]
    changed = dict(yaml, Version='v3.0.2', LicenseName='changed-license',
                   LicenseURL='Unknown', LicensePath='/module-cache/new-yaml/LICENSE')
    other = dict(yaml, Name='example.invalid/other', LicenseURL='Unknown')
    entries += [changed, other]
    with tempfile.TemporaryDirectory() as temporary:
        source = Path(temporary) / 'render.go'
        source.write_text(GO)
        result = subprocess.run(['go', 'run', str(source), str(ROOT / '.go-licenses-notice.tpl')],
                                input=json.dumps(entries), text=True, capture_output=True, check=True)
    rows = [line.split('\t') for line in result.stdout.splitlines()]
    expected = [yaml[key] for key in ('Name', 'Version', 'LicenseName', 'LicenseURL', 'LicensePath')]
    if rows[:4] != [expected] * 4 or len(set(result.stdout.splitlines()[:4])) != 1:
        raise AssertionError('target discovery failures must produce one unchanged YAML inventory row')
    if rows[4] != [changed['Name'], 'v3.0.2', 'changed-license',
                   'https://github.com/go-yaml/yaml/blob/v3.0.2/LICENSE', changed['LicensePath']]:
        raise AssertionError('version, detected license and actual license path must remain visible')
    if rows[5] != [other[key] for key in ('Name', 'Version', 'LicenseName', 'LicenseURL', 'LicensePath')]:
        raise AssertionError('unrecognized modules must not receive an invented source URL')
    check_installer()
    print('NOTICE_TEMPLATE_RESULT yaml_source=stable target_duplicates=deduplicated license_changes=preserved')
    print('NOTICE_INSTALL_RESULT transient=retried permanent=failed checksum_verification=preserved')


def check_installer():
    """Exercise the real generator with deterministic install transport failures."""
    for failures, expected_attempts in ((0, 1), (2, 3), (99, 3)):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            subprocess.run(['git', 'init', '-q', str(root)], check=True)
            shutil.copyfile(ROOT / 'scripts/gen-notice.sh', root / 'gen-notice.sh')
            fake_bin = root / 'bin'
            fake_bin.mkdir()
            for path, script in ((fake_bin / 'go', INSTALL_STUB), (root / 'report', REPORT_STUB)):
                path.write_text('#!' + sys.executable + '\n' + script)
                path.chmod(0o755)
            (root / 'license').write_text('Constructed license text\n')
            notice = root / 'NOTICE'
            notice.write_text('original inventory\n')
            env = dict(os.environ, PATH=str(fake_bin) + os.pathsep + os.environ['PATH'],
                       GOSUMDB='sum.golang.org', GONOSUMDB='', FAIL_INSTALLS=str(failures),
                       INSTALL_LOG=str(root / 'installs'), LICENSE_STUB=str(root / 'report'),
                       LICENSE_TEXT=str(root / 'license'))
            result = subprocess.run(['bash', 'gen-notice.sh'], cwd=root, env=env,
                                    text=True, capture_output=True)
            if (root / 'installs').read_text().splitlines() != ['install'] * expected_attempts:
                raise AssertionError('installer must bound retries: ' + result.stderr)
            if failures == 99:
                if result.returncode != 23 or notice.read_text() != 'original inventory\n':
                    raise AssertionError('persistent install failure must preserve failure and NOTICE')
            elif result.returncode or 'Constructed license text' not in notice.read_text():
                raise AssertionError('generator must recover from transient install failure: ' + result.stderr)


if __name__ == '__main__':
    main()
