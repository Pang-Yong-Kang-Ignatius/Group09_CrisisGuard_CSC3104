"""Audit retained summaries, backup contents, source lineage and recalculations."""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import zipfile

from evaluation.experiments.analyze_inheritance_results import audit_matrix

ROOT = Path(__file__).resolve().parents[2]


def digest(data):
    return hashlib.sha256(data).hexdigest()


def read(path):
    return json.loads((ROOT/path).read_text(encoding='utf-8'))


def source_lineage(hashes):
    result = {}
    for name, expected in hashes.items():
        revision = subprocess.run(['git', 'show', 'HEAD:'+name], cwd=ROOT, capture_output=True)
        if revision.returncode:
            result[name] = 'not present at reviewed HEAD'
            continue
        data = revision.stdout
        result[name] = 'matches HEAD blob' if expected == digest(data) else (
            'matches HEAD after CRLF normalization' if expected in (digest(data.replace(b'\r\n', b'\n')), digest(data.replace(b'\r\n', b'\n').replace(b'\n', b'\r\n'))) else 'different source revision; not reproduced')
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--backup', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    assert not args.output.exists(), 'Refuse to overwrite evidence'
    historical = ROOT/'evaluation/results/inheritance-matrix01'
    audit = audit_matrix(historical)['audit']
    assert audit['audit_status'] == 'passed' and audit['case_count'] == 40
    assert audit['dependency_urgent_improved_pairs'] == audit['dependency_urgent_eligible_pairs'] == 30
    with zipfile.ZipFile(args.backup) as backup:
        entries = [name for name in backup.namelist() if not name.endswith('/')]
        compared = {name: digest(backup.read(name)) == digest((historical/Path(name).name).read_bytes()) for name in entries}
    assert len(entries) == 5 and all(compared.values())
    fixed = read('docs/evidence/cp7b-fixed-vs-aimd-statistics-20261029.json')
    fixed_recomputed = read('evaluation/results/cp8-prior-fixed/statistics.json')
    fairness = read('docs/evidence/cp7b-sustained-fairness-statistics-20261029.json')
    fairness_recomputed = read('evaluation/results/cp8-prior-fairness/cp7b-sustained-fairness-statistics-20261029.json')
    telemetry = read('docs/evidence/cp7b-representative-telemetry-statistics-20261029.json')
    telemetry_recomputed = read('evaluation/results/cp8-prior-telemetry/cp7b-representative-telemetry-statistics-20261029.json')
    assert fixed == fixed_recomputed and fairness == fairness_recomputed
    # Evidence filenames and the absolute runner location can differ across exports.
    telemetry_counts = {key: telemetry[key] == telemetry_recomputed[key] for key in (
        'requests', 'metrics_samples', 'client_errors', 'non_200_responses', 'skipped_generator_opportunities',
        'all_expiry_windows_empty', 'all_live_replay_windows_classified', 'executor_capacity_unchanged', 'real_replay_extra_requests')}
    assert all(telemetry_counts.values())
    fair_raw = read('docs/evidence/cp7b-sustained-fairness-raw-20261029.json')
    manifest = read('evaluation/results/inheritance-matrix01/manifest.json')
    files = [historical/name for name in audit['input_sha256']]
    files += [ROOT/'docs/evidence'/name for name in (
        'cp7b-fixed-vs-aimd-raw-final-20261029.json', 'cp7b-sustained-fairness-raw-20261029.json',
        'cp7b-representative-telemetry-raw-20261029.json', 'cp7b-representative-telemetry-diagnostics-20261029.json',
        'cp7b-representative-telemetry-diagnostic-recovery-20261029.json')]
    result = {'reviewed_head': subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip(),
        'historical_summary_audit': audit, 'backup': str(args.backup.resolve()),
        'backup_sha256': digest(args.backup.read_bytes()), 'backup_entries_equal_retained_files': compared,
        'historical_event_traces': 'MISSING: backup contains only the five retained summaries',
        'historical_manifest_status': manifest['status'],
        'historical_source_lineage': source_lineage(manifest['source_file_sha256']),
        'fairness_source_lineage': source_lineage(fair_raw['source_sha256']),
        'fixed_statistics_recomputed_equal': fixed == fixed_recomputed,
        'fairness_statistics_recomputed_equal': fairness == fairness_recomputed,
        'telemetry_count_checks_equal': telemetry_counts,
        'input_sha256': {p.relative_to(ROOT).as_posix(): digest(p.read_bytes()) for p in files},
        'scope': 'Fresh summary/statistics recalculation only; no historical experiment reexecution or raw inheritance trace recovery'}
    args.output.write_text(json.dumps(result, indent=2), encoding='utf-8')
    print(json.dumps({k: v for k, v in result.items() if k not in ('input_sha256', 'fairness_source_lineage', 'historical_source_lineage', 'backup_entries_equal_retained_files')}, indent=2))


if __name__ == '__main__':
    main()
