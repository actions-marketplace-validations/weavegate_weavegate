#!/usr/bin/env python3
"""Check NOTICE source normalization using the actual Go text template."""
import json
import subprocess
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
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
    print('NOTICE_TEMPLATE_RESULT yaml_source=stable target_duplicates=deduplicated license_changes=preserved')


if __name__ == '__main__':
    main()
