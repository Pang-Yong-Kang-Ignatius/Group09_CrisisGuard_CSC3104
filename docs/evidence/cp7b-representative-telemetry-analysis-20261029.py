"""Reproduce telemetry boundary calibration tables and report figures."""
import argparse
import csv
from datetime import datetime
import hashlib
import importlib.util
import json
from pathlib import Path
import statistics
import sys

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import numpy as np


HERE=Path(__file__).resolve().parent
SERVICES=('patient','emergency','admin')
PHASES=('live_6rps','live_24rps','mixed_live_replay','db_wait_low_concurrency','executor_wait_bounded','upstream_delay_300ms')
LABELS=('Live 6 RPS','Live 24 RPS','Live + replay','DB lock / low C','DB lock / C≤64','Upstream +300ms')


def stamp(value):
    return datetime.fromisoformat(value).timestamp()


def p95(values):
    values=sorted(values)
    return values[(95*len(values)+99)//100-1] if values else None


def describe(values):
    values=[v for v in values if v is not None]
    return {'n':len(values),'mean':statistics.fmean(values),'sd':statistics.stdev(values) if len(values)>1 else 0,
            'min':min(values),'max':max(values)} if values else {'n':0,'mean':None,'sd':None,'min':None,'max':None}


def export(path,rows):
    with path.open('w',newline='',encoding='utf-8') as f:
        writer=csv.DictWriter(f,fieldnames=list(rows[0]))
        writer.writeheader();writer.writerows(rows)


def summarize(trial,phase,service):
    definition=phase['definition']
    records=[r for r in phase['requests'] if r['service']==service]
    snapshots=[m for m in phase['metrics_samples'] if m['service']==service]
    post_metric=phase['end_metrics'][service]
    last_client_finish=max(stamp(r['finished_at']) for r in records)
    # Replay drain can outlast the read generator. Compare a real snapshot
    # from the active read interval rather than an expired post-drain window.
    candidates=[m for m in snapshots if stamp(m['sampled_at'])<=last_client_finish]
    metric=max(candidates,key=lambda m:stamp(m['sampled_at'])) if candidates else post_metric
    baseline=phase['baseline'][service]
    observed=stamp(metric['sampled_at'])
    client_window=[r for r in records if observed-60<=stamp(r['finished_at'])<=observed]
    live=[r for r in client_window if r['kind']=='live']
    replay=[r for r in client_window if r['kind']=='replay']
    offered=next(r for r in phase['offered_load'] if r['service']==service)
    times=sorted(stamp(m['sampled_at']) for m in snapshots)
    return {
        'trial':trial['trial'],'phase':definition['name'],'diagnostic':definition['diagnostic'],'service':service,
        'evidence_project':phase.get('evidence_project',trial['project']),
        'comparison_metric_sampled_at':metric['sampled_at'],
        'requests':len(records),'live_requests':sum(r['kind']=='live' for r in records),'replay_get_requests':sum(r['kind']=='replay' for r in records),
        'offered_generator_opportunities':offered['offered'],'skipped_generator_opportunities':sum(offered['skipped_generator_opportunities'].values()),
        'client_concurrency_cap':offered['client_concurrency_cap'],'delivered_client_rps':len(records)/offered['duration_seconds'],
        'client_errors':sum(r['error'] is not None for r in records),'non_200_responses':sum(r['status_code']!=200 for r in records),
        'client_live_full_phase_p95_ms':p95([r['client_latency_ms'] for r in records if r['kind']=='live']),
        'client_live_window_p95_ms':p95([r['client_latency_ms'] for r in live]),
        'client_replay_window_p95_ms':p95([r['client_latency_ms'] for r in replay]),
        'client_live_window_count':len(live),'client_replay_window_count':len(replay),
        'middleware_live_p95_ms':metric['live_p95_latency_ms'],'middleware_mixed_p95_ms':metric['p95_latency_ms'],
        'middleware_mixed_mean_ms':metric['average_latency_ms'],'middleware_throughput_rps':metric['throughput_rps'],
        'middleware_live_count':metric['live_latency_sample_count'],'middleware_replay_count':metric['replay_latency_sample_count'],
        'middleware_unclassified_count':metric['unclassified_latency_sample_count'],'middleware_total_count':metric['latency_sample_count'],
        'middleware_total_delta':post_metric['total_requests']-baseline['total_requests'],
        'middleware_extra_replay_requests':post_metric['total_requests']-baseline['total_requests']-len(records),
        'middleware_failed_delta':post_metric['failed_requests']-baseline['failed_requests'],'middleware_error_rate':metric['error_rate'],
        'executor_worker_limit':metric['executor_worker_limit'],'peak_active_workers':max(m['executor_active_workers'] for m in snapshots),
        'peak_executor_waiting':max(m['executor_queue_depth'] for m in snapshots),
        'peak_executor_pressure':max(m['executor_queue_pressure'] for m in snapshots),
        'positive_queue_observations':sum(m['executor_queue_depth']>0 for m in snapshots),'metric_samples':len(snapshots),
        'metric_fetch_p95_ms':p95([m['fetch_duration_ms'] for m in snapshots]),
        'metric_fetch_max_ms':max(m['fetch_duration_ms'] for m in snapshots),
        'maximum_source_sample_gap_s':max((b-a for a,b in zip(times,times[1:])),default=0),
        'controller_states':'>'.join(dict.fromkeys(m['state'] for m in phase['controller_samples'])),
        'controller_min_rate_rps':min(m['rate_rps'] for m in phase['controller_samples']),
        'controller_max_rate_rps':max(m['rate_rps'] for m in phase['controller_samples']),
        'controller_unfresh_observations':sum(not m['telemetry_fresh'] for m in phase['controller_samples']),
        'expiry_live_count':phase['expiry'][service]['live_latency_sample_count'],
        'expiry_live_p95_ms':phase['expiry'][service]['live_p95_latency_ms'],
        'expiry_queue_depth':phase['expiry'][service]['executor_queue_depth'],
    }


def plot_latencies(rows,path):
    fig,axes=plt.subplots(1,3,figsize=(14,5.2),sharey=True,constrained_layout=True)
    for ax,service in zip(axes,SERVICES):
        for key,label,color,offset in (
            ('middleware_live_p95_ms','Middleware live p95','#2d70a8',-.13),
            ('client_live_window_p95_ms','Client live GET p95','#db7731',0),
            ('middleware_mixed_mean_ms','Middleware mixed mean','#4a9a65',.13)):
            summaries=[describe([r[key] for r in rows if r['service']==service and r['phase']==phase]) for phase in PHASES]
            ax.errorbar(np.arange(len(PHASES))+offset,[s['mean'] for s in summaries],yerr=[s['sd'] for s in summaries],
                        fmt='o',capsize=3,color=color,label=label,ms=4)
        ax.set_title(service.title())
        ax.set_xticks(range(len(PHASES)),LABELS,rotation=35,ha='right',fontsize=8)
        ax.axvline(2.5,color='#777777',ls='--',lw=1)
        ax.set_yscale('log');ax.grid(axis='y',alpha=.2,which='both')
    axes[0].set_ylabel('Latency (ms, log scale)')
    axes[0].legend(fontsize=8,loc='upper left')
    fig.suptitle('Latency boundaries under sustained traffic and separately labelled diagnostic perturbations')
    fig.savefig(path,dpi=180);plt.close(fig)


def plot_executor(trials,path):
    fig,axes=plt.subplots(2,3,figsize=(13,7),sharex=True,sharey=True,constrained_layout=True)
    for col,trial in enumerate(trials):
        for row,name in enumerate(('db_wait_low_concurrency','executor_wait_bounded')):
            phase=next(p for p in trial['phases'] if p['definition']['name']==name)
            ax=axes[row,col];start=stamp(phase['started_at'])
            metrics=[m for m in phase['metrics_samples'] if m['service']=='patient']
            x=[stamp(m['sampled_at'])-start for m in metrics]
            for pulse in phase['lock_pulses']:
                ax.axvspan(stamp(pulse['lock_acquired_at'])-start,stamp(pulse['released_at'])-start,color='#888888',alpha=.14)
            ax.scatter(x,[m['executor_queue_depth'] for m in metrics],s=12,color='#db7731',label='Observed tasks waiting')
            ax.scatter(x,[m['executor_active_workers'] for m in metrics],s=9,color='#2d70a8',marker='x',label='Observed active workers')
            ax.axhline(40,color='#555555',ls='--',lw=1,label='Unchanged capacity: 40')
            ax.set_title(f"Trial {trial['trial']} / {'low concurrency' if row==0 else 'bounded concurrency ≤64'}")
            ax.grid(alpha=.2)
            if col==0:ax.set_ylabel('Tasks / workers (count)')
            if row==1:ax.set_xlabel('Elapsed diagnostic phase time (s)')
    axes[0,0].legend(fontsize=8)
    fig.suptitle('Patient executor observations during real disposable-DB table-lock pulses (shaded)')
    fig.savefig(path,dpi=180);plt.close(fig)


def plot_populations(rows,path):
    fig,axes=plt.subplots(1,3,figsize=(12,4.4),sharey=True,constrained_layout=True)
    for ax,service in zip(axes,SERVICES):
        own=[r for r in rows if r['phase']=='mixed_live_replay' and r['service']==service]
        x=np.arange(len(own))
        ax.bar(x,[r['middleware_live_count'] for r in own],label='Authenticated live',color='#2d70a8')
        ax.bar(x,[r['middleware_replay_count'] for r in own],bottom=[r['middleware_live_count'] for r in own],
               label='Authenticated replay',color='#db7731')
        ax.set_xticks(x,[f"Trial {r['trial']}" for r in own]);ax.set_title(service.title())
        ax.grid(axis='y',alpha=.2)
    axes[0].set_ylabel('Completed requests / 60 s')
    axes[0].legend(fontsize=8)
    fig.suptitle('Mixed workload populations: live reads, replay reads and actual hospital replay writes')
    fig.savefig(path,dpi=180);plt.close(fig)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('raw',type=Path);parser.add_argument('--outdir',type=Path,required=True)
    parser.add_argument('--supplement',type=Path,action='append',default=[],help='Diagnostic supplement/recovery files, repeat as needed; original outcomes are retained')
    args=parser.parse_args()
    raw=json.loads(args.raw.read_text(encoding='utf-8'))
    evidence_files=[{'path':str(args.raw),'sha256':hashlib.sha256(args.raw.read_bytes()).hexdigest(),'recorded_result':raw['result'],
                    'recorded_preservation':raw['preservation']}]
    phase_lineage=[]
    if args.supplement:
        reports=[(args.raw,raw)]
        for path in args.supplement:
            supplement=json.loads(path.read_text(encoding='utf-8'))
            assert raw['source_sha256']==supplement['source_sha256']
            assert raw['baseline_containers']==supplement['baseline_containers']
            assert raw['baseline_volumes']==supplement['baseline_volumes']
            assert supplement['preservation']['application_sources_equal']
            assert all(t['cleanup']=='passed' for t in supplement['trials'])
            assert all(t['result']=='passed' or 'Replay did not finish within bound' in t.get('error','') for t in supplement['trials'])
            reports.append((path,supplement))
            evidence_files.append({'path':str(path),'sha256':hashlib.sha256(path.read_bytes()).hexdigest(),'recorded_result':supplement['result'],
                                   'recorded_preservation':supplement['preservation']})
        assert raw['result']=='failed' and len(raw['trials'])==3
        # A separate recovery can overlap other disposable projects, making
        # their intermediate global resource-set equality false. Do not hide
        # that raw result. Require exact original equality at both endpoints.
        terminal=reports[-1][1]
        assert terminal['result']=='passed'
        for report in (raw,terminal):
            assert all(report['preservation'][key] for key in ('container_identity_image_mounts_equal','volume_set_equal','application_sources_equal'))
        combined=[]
        for original in raw['trials']:
            assert 'ProactorEventLoop' in original.get('error','') and original['cleanup']=='passed'
            phases=[]
            for name in PHASES:
                if name in PHASES[:3]:
                    owner,source=original,args.raw
                else:
                    choices=[(t,path) for path,report in reports[1:] for t in report['trials']
                             if t['trial']==original['trial'] and t['result']=='passed' and t['cleanup']=='passed'
                             and any(p['definition']['name']==name and 'expiry' in p for p in t['phases'])]
                    assert choices,f'Missing completed diagnostic replica {original["trial"]}: {name}'
                    owner,source=choices[0]
                chosen=next(p for p in owner['phases'] if p['definition']['name']==name)
                assert 'expiry' in chosen and 'client_summary' in chosen and 'ended_at' in chosen
                phases.append({**chosen,'evidence_project':owner['project']})
                phase_lineage.append({'trial':owner['trial'],'phase':name,'project':owner['project'],
                    'source':str(source),'validated_phase_complete':True,
                    'diagnostic_fixture_seed':owner.get('integrity',{}).get('diagnostic_fixture_seed',False)})
            combined.append({**original,'phases':phases})
        # This is a derived phase-level analysis, never a rewrite or promotion
        # of the interrupted raw run's failed result.
        raw={**raw,'trials':combined}
    else:
        assert raw['result']=='passed'
        phase_lineage=[{'trial':t['trial'],'phase':p['definition']['name'],'project':t['project'],
                       'source':str(args.raw),'validated_phase_complete':True} for t in raw['trials'] for p in t['phases']]
    rows=[summarize(t,p,s) for t in raw['trials'] for p in t['phases'] for s in SERVICES]
    assert len(rows)==54
    assert all(r['middleware_live_count']+r['middleware_replay_count']+r['middleware_unclassified_count']==r['middleware_total_count'] for r in rows)
    assert all(r['middleware_replay_count']==0 and r['middleware_live_count']==r['middleware_total_count'] for r in rows if r['phase']!='mixed_live_replay')
    assert all(r['middleware_live_count']>0 and r['middleware_replay_count']>0 for r in rows if r['phase']=='mixed_live_replay')
    prefix='cp7b-representative-telemetry-'
    export(args.outdir/(prefix+'summary-20261029.csv'),rows)
    snapshots=[];requests=[]
    for trial in raw['trials']:
        for phase in trial['phases']:
            project=phase.get('evidence_project',trial['project'])
            requests.extend({'trial':trial['trial'],'evidence_project':project,**r} for r in phase['requests'])
            for m in phase['metrics_samples']:
                snapshots.append({'trial':trial['trial'],'evidence_project':project,'phase':phase['definition']['name'],
                    **{k:m[k] for k in ('service','sampled_at','received_at','fetch_duration_ms','live_p95_latency_ms',
                    'average_latency_ms','throughput_rps','error_rate','live_latency_sample_count','replay_latency_sample_count',
                    'unclassified_latency_sample_count','executor_queue_depth','executor_active_workers','executor_worker_limit','executor_queue_pressure')}})
    export(args.outdir/(prefix+'requests-20261029.csv'),requests)
    export(args.outdir/(prefix+'metrics-20261029.csv'),snapshots)
    assert all(abs(s['executor_queue_pressure']-s['executor_queue_depth']/s['executor_worker_limit'])<1e-9 for s in snapshots)
    fields=('middleware_live_p95_ms','client_live_window_p95_ms','middleware_mixed_mean_ms','middleware_throughput_rps',
            'middleware_live_count','middleware_replay_count','peak_active_workers','peak_executor_waiting','peak_executor_pressure',
            'metric_fetch_max_ms','delivered_client_rps','skipped_generator_opportunities')
    aggregate={phase:{service:{field:describe([r[field] for r in rows if r['phase']==phase and r['service']==service])
                              for field in fields} for service in SERVICES} for phase in PHASES}
    stats={'aggregate':aggregate,'requests':len(requests),'metrics_samples':len(snapshots),
        'evidence_files':evidence_files,'phase_lineage':phase_lineage,
        'completed_phase_replicas':len(phase_lineage),
        'client_errors':sum(r['error'] is not None for r in requests),'non_200_responses':sum(r['status_code']!=200 for r in requests),
        'skipped_generator_opportunities':sum(r['skipped_generator_opportunities'] for r in rows),
        'all_expiry_windows_empty':all(r['expiry_live_count']==0 and r['expiry_live_p95_ms'] is None and r['expiry_queue_depth']==0 for r in rows),
        'all_live_replay_windows_classified':all(r['middleware_unclassified_count']==0 for r in rows),
        'executor_capacity_unchanged':all(r['executor_worker_limit']==40 for r in rows),
        'real_replay_extra_requests':sum(r['middleware_extra_replay_requests'] for r in rows),
        'note':'Means and sample SD use three project replicates. Comparison uses the latest periodic middleware snapshot at/before the final generated client completion, with client completion windows aligned approximately by UTC; full response time and route populations still differ. Diagnostic locks and proxy delay are synthetic perturbations, not representative capacity measurements.'}
    (args.outdir/(prefix+'statistics-20261029.json')).write_text(json.dumps(stats,indent=2),encoding='utf-8')
    (args.outdir/(prefix+'evidence-index-20261029.json')).write_text(json.dumps({
        'raw_runs':evidence_files,'completed_phase_replicas':phase_lineage,
        'interrupted_phases_included':False,'original_results_preserved':True},indent=2),encoding='utf-8')
    figures=args.outdir/'figures';figures.mkdir(exist_ok=True)
    plot_latencies(rows,figures/(prefix+'latency-boundaries-20261029.png'))
    plot_executor(raw['trials'],figures/(prefix+'executor-waiting-20261029.png'))
    plot_populations(rows,figures/(prefix+'mixed-populations-20261029.png'))
    print(json.dumps({k:stats[k] for k in ('requests','metrics_samples','client_errors','non_200_responses','skipped_generator_opportunities',
        'all_expiry_windows_empty','all_live_replay_windows_classified','executor_capacity_unchanged','real_replay_extra_requests')},indent=2))


if __name__=='__main__':main()
