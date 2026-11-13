"""Reproduce the single-pair retry figure, checking assessment and raw evidence."""
import argparse
import csv
import hashlib
import json
import math
from pathlib import Path

from docs.evidence.analyze_aimd_deferral_pilot_20261101 import review

ROOT = Path(__file__).resolve().parents[2]
PREFIX = 'cp8-aimd-bounded-deferral-pilot-20261101'
ASSESSMENT = ROOT/'docs/evidence/aimd-deferral-pilot-review-20261101.json'


def verified_data():
    saved = json.loads(ASSESSMENT.read_text())
    sources = [ROOT/Path(t['source']) for t in saved['trials'].values()]
    assert len({p.parent.parent for p in sources}) == 1
    current = review(sources[0].parent.parent.relative_to(ROOT))
    assert current == {k:v for k,v in saved.items() if k != 'script_sha256'}
    analyzer = ROOT/'docs/evidence/analyze_aimd_deferral_pilot_20261101.py'
    assert hashlib.sha256(analyzer.read_bytes()).hexdigest() == saved['script_sha256']
    assert all(hashlib.sha256(Path(p).read_bytes()).hexdigest() == h for p,h in saved['source_sha256'].items())
    rows = []
    for policy in ('failure-backoff', 'bounded-deferral'):
        trial = saved['trials'][policy]
        row = dict(retry_policy=policy, admission_policy='aimd', health_policy='pressure-experimental',
            paired_seed=60202, paired_seed_count=1, live_read_rps=84, original_backlog_events=88,
            drain_observer_lower_seconds=trial['drain_interval_seconds'][0],
            drain_observer_upper_seconds=trial['drain_interval_seconds'][1],
            full_run_attempts=trial['full_run']['attempts'], full_run_violations=trial['full_run']['violations'],
            full_run_slo_percent=100*trial['full_run']['fraction'],
            fixed_post_ready_window_seconds=10, fixed_post_ready_attempts=trial['fixed10']['attempts'],
            fixed_post_ready_violations=trial['fixed10']['violations'],
            generator_issuance_ratio=trial['generator']['issuance_ratio'], source_summary=trial['source'])
        for population in trial['workflow_p95']:
            site = population['site']
            for kind in ('urgent','routine'):
                row[site+'_'+kind+'_p95_seconds'] = population[kind+'_workflow_p95_upper_seconds']
        rows.append(row)
    reduction = 100*(1-rows[1]['drain_observer_upper_seconds']/rows[0]['drain_observer_upper_seconds'])
    assert math.isclose(reduction, -saved['effects']['drain_relative_pct'], abs_tol=1e-10)
    return saved, rows, reduction


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output-dir', type=Path, required=True)
    args = parser.parse_args()
    data, rows, reduction = verified_data()
    paths = {key:args.output_dir/(PREFIX+extension) for key,extension in
             (('png','.png'),('pdf','.pdf'),('csv','-data.csv'),('caption','-caption.md'),('provenance','-provenance.json'))}
    if any(p.exists() for p in paths.values()):
        raise FileExistsError('Existing evidence is never overwritten; choose a new output directory')
    args.output_dir.mkdir(parents=True, exist_ok=True)
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    plt.rcParams.update({'font.family':'DejaVu Sans','font.size':11,'pdf.fonttype':42,'ps.fonttype':42})
    fig, ax = plt.subplots(figsize=(8,5.1))
    fig.subplots_adjust(left=.28, right=.96, top=.78, bottom=.33)
    values = [r['drain_observer_upper_seconds'] for r in rows]
    ax.barh([1,0], values, height=.43, color=['#676F7D','#0072B2'], zorder=3)
    ax.set_yticks([1,0], ['Failure-backoff retry','Bounded-deferral retry'])
    ax.set_xlim(0,76)
    ax.set_ylim(-.6,1.6)
    ax.set_xticks(range(0,71,10))
    ax.set_xlabel('Original-backlog drain time (seconds)',labelpad=10)
    ax.grid(axis='x',color='#DFE3E8',linewidth=.7,zorder=0)
    ax.tick_params(axis='both',length=0,pad=8)
    for spine in ('top','right','left'):
        ax.spines[spine].set_visible(False)
    ax.spines['bottom'].set_color('#9AA2AD')
    for y,value in zip((1,0),values):
        ax.text(value+1.2,y,f'{value:.3f} s',va='center',fontsize=11,color='#202B39')
    fig.text(.06,.95,'Recovery with experimental AIMD',fontsize=16,weight='bold',color='#202B39')
    fig.text(.06,.89,'Single paired pilot at 84 read requests/s, seed 60202; 88-event backlog',fontsize=10)
    ax.text(.98,1.14,f'Observed drain reduction: {reduction:.2f}%',transform=ax.transAxes,
            ha='right',fontsize=12,weight='bold',color='#202B39')
    fig.text(.06,.18,'One paired seed; no statistical significance established. No statistical error bars.',fontsize=9)
    fig.text(.06,.12,'Adverse results: Hospital B routine p95 +40.95%; full-run SLO violations +1.726 pp.',fontsize=9)
    fig.text(.06,.06,'First 10 s after recorded Cloud readiness: 0/840 observed violations in each arm.',fontsize=9)
    fig.savefig(paths['png'],dpi=300,facecolor='white')
    fig.savefig(paths['pdf'],facecolor='white',metadata={'Title':'Experimental AIMD bounded-deferral pilot',
        'Author':'CrisisGuard evaluation','Subject':'Single paired seed 60202; no significance claim'})
    plt.close(fig)
    with paths['csv'].open('x',newline='',encoding='utf-8') as f:
        writer=csv.DictWriter(f,fieldnames=list(rows[0]));writer.writeheader();writer.writerows(rows)
    caption = f'''# Experimental AIMD retry comparison: single paired pilot

Bars show original-backlog Cloud drain upper observer endpoints: failure backoff
{values[0]:.3f} s and opt-in bounded admission-deferral retry {values[1]:.3f} s,
an observed {reduction:.2f}% reduction. Both arms used AIMD, `pressure-experimental`,
84 authenticated Patient read requests/s, the same 88-event balanced dependency
graph and paired seed 60202. There is one pair, not a statistical sample across
seeds; no statistical significance or confidence interval is established. No
statistical error bars are drawn. Lower observer endpoints accompany the CSV
and are measurement bounds, not statistical uncertainty intervals.

Adverse results remain part of this comparison: Hospital B routine workflow p95
worsened **40.95%** (24.000 to 33.828 s), and full-run end-to-end live SLO violation
rate increased **1.726 percentage points** (99/5,325 = 1.859% to 102/2,845 = 3.585%).
Both predefined first 10-second windows after each trial's recorded all-Cloud
readiness had **zero observed violations among 840 issued requests**; successful
client p95 was approximately 16 ms. Faster drain shortened the full-run denominator
while startup violation counts changed from 99 to 102. The fixed-window result
does not replace the full-run outcome or demonstrate protection under overload.
End-to-end violations are non-200 responses, transport failures or latency >250 ms,
counted once per issued request. Both generators had zero skips and exact ledgers.

These experimental policies require explicit opt-in; production defaults and the
original negative H1/H2 findings are unchanged. Absolute 84 RPS is not a maximum
mixed-workload capacity claim. The pilot has no constant-rate or RR arm and does
not establish Appendix D3 acceptance. A compatible final-policy constant-rate
comparison, independent holdout and separate RR urgent comparison remain necessary.

Source assessment: `docs/evidence/aimd-deferral-pilot-review-20261101.json`.
Original summaries: `evaluation/results/aimd-deferral-pilot-20261101-202315/run-{{failure-backoff,bounded-deferral}}/summary.json`.
The builder recomputes the assessment, checks both raw archives against summaries,
and verifies source hashes before plotting. The CSV retains the actual measured
values; rounding to three decimals is display-only.

Reproduce from the repository root into a new directory:

```powershell
.\\.venv311\\Scripts\\python.exe -m docs.evidence.plot_aimd_deferral_pilot_20261101 --output-dir evaluation/results/NEW-deferral-figure
```
'''
    paths['caption'].write_text(caption,encoding='utf-8')
    provenance = dict(assessment=str(ASSESSMENT.relative_to(ROOT)),
        assessment_sha256=hashlib.sha256(ASSESSMENT.read_bytes()).hexdigest(), source_sha256=data['source_sha256'],
        builder_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        observed_reduction_percent=reduction, n_pairs=1, statistical_interval=None,
        output_sha256={p.name:hashlib.sha256(p.read_bytes()).hexdigest() for key,p in paths.items() if key!='provenance'})
    paths['provenance'].write_text(json.dumps(provenance,indent=2)+'\n',encoding='utf-8')
    print('Verified raw evidence and generated '+str(paths['png']))


if __name__=='__main__':
    main()
