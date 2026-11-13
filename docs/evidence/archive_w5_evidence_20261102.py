"""Preserve local W5 raw evidence without private Compose files/command logs."""
import hashlib
import json
from pathlib import Path
import zipfile

ROOT = Path(__file__).resolve().parents[2]


def main():
    target = ROOT / 'evaluation/results/w5-evidence-20261102.zip'
    entries = {}
    for name in ('w5-holdout-20261102', 'w5-holdout-20261102-r2', 'w5-holdout-20261102-r3',
                 'w5-replacement-60103-20261102'):
        folder = ROOT / 'evaluation/results' / name
        for path in folder.rglob('*'):
            if path.is_file() and path.suffix in ('.json', '.log'):
                entries[path.relative_to(ROOT).as_posix()] = path
        for summary in folder.rglob('summary.json'):
            d = json.loads(summary.read_text())
            for t in d['trials']:
                raw = Path(t['raw_path'])
                entries['raw/' + t['project'] + '/raw.json'] = raw
                for name in ('postgres-tests.xml', 'pytest.log'):
                    path = raw.parent / name
                    if path.exists():
                        entries['raw/' + t['project'] + '/' + name] = path
    selected = json.loads((ROOT / 'evaluation/results/w5-holdout-20261102-r3/plan-36-60101-constant/plan.json').read_text())
    for name, digest in selected['source_sha256'].items():
        path = ROOT / name
        assert hashlib.sha256(path.read_bytes()).hexdigest() == digest
        entries['frozen-source/' + name] = path
    for path in (ROOT / 'evaluation/results').glob('w5-*'):
        if path.is_file() and path.suffix in ('.txt', '.xml', '.json'):
            entries[path.relative_to(ROOT).as_posix()] = path
    for path in (ROOT / 'docs/evidence').glob('*w5*20261102*'):
        if path.is_file():
            entries[path.relative_to(ROOT).as_posix()] = path
    for name in ('requirements.txt', 'infrastructure/docker/Dockerfile',
                 'docs/reports/W5_AIMD_RECOVERY_VALIDATION_20261102.md',
                 'tests/unit/test_w5_database_retention.py'):
        entries['frozen-source/' + name] = ROOT / name
    hashes = {name: hashlib.sha256(path.read_bytes()).hexdigest() for name, path in entries.items()}
    with zipfile.ZipFile(target, 'x', compression=zipfile.ZIP_DEFLATED) as z:
        for name, path in entries.items():
            z.write(path, name)
        z.writestr('SHA256-MANIFEST.json', json.dumps(hashes, indent=2))
    with zipfile.ZipFile(target) as z:
        assert z.testzip() is None
        for name, digest in hashes.items():
            assert hashlib.sha256(z.read(name)).hexdigest() == digest
    manifest = dict(archive=str(target), bytes=target.stat().st_size,
                    sha256=hashlib.sha256(target.read_bytes()).hexdigest(), entries=len(hashes),
                    archive_readback_verified=True,
                    excludes=['private Compose configurations', 'commands.log', 'credentials', 'database volume binaries'],
                    retention='All test database volumes remain in Docker; local archive is not an off-machine backup.')
    (ROOT / 'docs/evidence/w5-archive-20261102.json').write_text(json.dumps(manifest, indent=2))
    print(json.dumps(manifest, indent=2))


if __name__ == '__main__':
    main()
