"""One prospectively bounded replacement for a generator-invalid AIMD arm."""
import hashlib
import json
from pathlib import Path
import subprocess
import sys

from docs.evidence.run_w5_holdout_20261102 import COMMON, ROOT
from evaluation.experiments.analyze_recovery_matrix import generator_accuracy


def main():
    original = ROOT / 'evaluation/results/w5-holdout-20261102-r3/run-84-60103-aimd/summary.json'
    control = original.parent.parent / 'run-84-60103-constant/summary.json'
    trial = json.loads(original.read_text())['trials'][0]
    g = generator_accuracy(trial)
    assert g['ledger_reconciled'] and g['skip_fraction'] > .001
    output = ROOT / 'evaluation/results/w5-replacement-60103-20261102'
    output.mkdir(exist_ok=False)
    protocol = dict(reason='Original AIMD arm invalid under prospectively frozen .001 generator rule; 117 cap and 1 lag skips.',
                    original_source=str(original), original_sha256=hashlib.sha256(original.read_bytes()).hexdigest(),
                    original_generator=g, paired_control=str(control),
                    control_sha256=hashlib.sha256(control.read_bytes()).hexdigest(),
                    maximum_replacement_attempts=1, selection='Keep 19 original arms plus this replacement only if generator-valid; otherwise 84 RPS remains unestablished.',
                    limitation='Replacement planned after original outcomes; valid control acquired earlier. Preserve original invalid arm and report retrospective pairing.',
                    unchanged='No controller, measurement, policy, graph, generator limit or threshold changes.')
    (output / 'replacement-protocol.json').write_text(json.dumps(protocol, indent=2))
    args = COMMON + ['--pilot-seeds', '60103', '--live-read-rps', '84', '--admission-mode', 'aimd']
    cmd = [sys.executable, '-m', 'evaluation.experiments.run_recovery_matrix', *args]
    subprocess.run(cmd + ['--plan-only', '--output', str(output / 'plan')], cwd=ROOT, check=True)
    with (output / 'run.log').open('w') as log:
        result = subprocess.run(cmd + ['--output', str(output / 'run')], cwd=ROOT,
                                stdout=log, stderr=subprocess.STDOUT)
    raise SystemExit(result.returncode)


if __name__ == '__main__':
    main()
