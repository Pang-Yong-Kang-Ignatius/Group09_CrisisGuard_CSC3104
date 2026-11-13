"""Frozen W5 holdout orchestration; existing runner supplies all measurements."""
import hashlib
import argparse
import json
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / 'evaluation/results/w5-holdout-20261102'
COMMON = ['--mode', 'pilot', '--pilot-policy', 'bounded_on', '--total-events', '88',
          '--activation', 'automatic', '--admission-ceiling', '20',
          '--admission-health-policy', 'pressure-experimental',
          '--metaguard-retry-policy', 'bounded-deferral', '--live-read-target', 'preseed',
          '--live-workers', '128', '--live-slo-ms', '250', '--timeout', '300',
          '--workers', '1', '--retain-test-databases']


def main():
    global OUT
    parser = argparse.ArgumentParser()
    parser.add_argument('--output-name', default='w5-holdout-20261102')
    args = parser.parse_args()
    assert Path(args.output_name).name == args.output_name
    OUT = ROOT / 'evaluation/results' / args.output_name
    OUT.mkdir(exist_ok=False)
    jobs = []
    for seed in range(60101, 60106):
        for load in (36, 84):
            arms = ['constant', 'aimd'] if (seed + (load == 84)) % 2 else ['aimd', 'constant']
            for arm in arms:
                name = f'{load}-{seed}-{arm}'
                args = COMMON + ['--pilot-seeds', str(seed), '--live-read-rps', str(load), '--admission-mode', arm]
                jobs.append(dict(name=name, seed=seed, load=load, arm=arm, args=args))
    protocol = dict(source_commit='58f987b46b65bf6f214909dd0d696d469e8cec39', jobs=jobs,
                    policy_frozen_before_outcomes=True, independent_seeds=list(range(60101, 60106)),
                    targets=dict(drain_max_pct=20, routine_each_site_max_pct=20, live_slo_max_pp=1),
                    generator=dict(minimum_issuance=.999, sensitivity=[.9999, 1.0]),
                    primary_slo='non-200 OR transport failure OR latency >250ms / all attempted requests',
                    secondary_slo='first 10 seconds of requests started after all-Cloud readiness; unavailable if exposure shorter',
                    limitations=['Provisional 120 RPS read-only reference; 36/84 RPS are absolute loads.',
                                 'No representative mixed capacity, genuine overload or full D2 claim.',
                                 'Independent Haris owner review remains outstanding.',
                                 'Five pairs per load; minimum exact two-sided p=.0625.'],
                    database_retention='All test PostgreSQL/SQLite volumes retained; no database deletion.')
    (OUT / 'protocol.json').write_text(json.dumps(protocol, indent=2))
    for job in jobs:
        cmd = [sys.executable, '-m', 'evaluation.experiments.run_recovery_matrix', *job['args']]
        subprocess.run(cmd + ['--plan-only', '--output', str(OUT / ('plan-' + job['name']))], cwd=ROOT, check=True)
    plans = {str(p.relative_to(OUT)): hashlib.sha256(p.read_bytes()).hexdigest() for p in OUT.glob('plan-*/plan.json')}
    (OUT / 'plan-hashes.json').write_text(json.dumps(plans, indent=2))
    for job in jobs:
        if (OUT / 'STOP').exists():
            raise SystemExit('Stopped before next trial; existing evidence retained')
        print('START ' + job['name'], flush=True)
        with (OUT / (job['name'] + '.log')).open('w') as log:
            result = subprocess.run([sys.executable, '-m', 'evaluation.experiments.run_recovery_matrix',
                                     *job['args'], '--output', str(OUT / ('run-' + job['name']))],
                                    cwd=ROOT, stdout=log, stderr=subprocess.STDOUT)
        print(f'FINISH {job["name"]} exit={result.returncode}', flush=True)
        if result.returncode:
            raise SystemExit('Failed trial; preserve logs and stop expansion')


if __name__ == '__main__':
    main()
