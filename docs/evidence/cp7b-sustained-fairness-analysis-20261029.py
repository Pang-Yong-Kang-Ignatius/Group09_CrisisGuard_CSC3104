"""Reproduce fairness summaries and scientific figures from recorded evidence."""
from __future__ import annotations

import argparse
import csv
from datetime import datetime
import json
from pathlib import Path
import statistics

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

SITES=("Hospital_A","Hospital_B")
COLORS={SITES[0]:"#2867a0",SITES[1]:"#d96c28"}


def timestamp(value):
    return datetime.fromisoformat(value).timestamp()


def p95(values):
    values=sorted(values)
    return values[(95*len(values)+99)//100-1] if values else None


def describe(values):
    return {"n":len(values),"mean":statistics.fmean(values),"median":statistics.median(values),
            "sd":statistics.stdev(values) if len(values)>1 else 0,"min":min(values),"max":max(values)}


def export_csv(path,rows):
    with path.open("w",encoding="utf-8",newline="") as f:
        writer=csv.DictWriter(f,fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def inspect_trial(trial,sustained_seconds):
    start=timestamp(trial["recovery_start_at"])
    cloud={e["event_id"]:e for e in trial["postgres_events"]}
    local={e["event_id"]:e for s in SITES for e in trial["sqlite"][s]["events"]}
    recovery={e["event_id"]:e for s in SITES for e in trial["sqlite"][s]["recovery"]}
    allocations=[e for e in trial["audit"] if e["action"]=="allocate"]
    event_rows=[]
    for event_id,event in cloud.items():
        created=timestamp(local[event_id]["created_at"])
        applied=timestamp(event["applied_at"])
        ready=max([created]+[timestamp(cloud[parent]["applied_at"]) for parent in event["prerequisite_ids"]])
        own_grants=[g for g in trial["grants"] if g["event_id"]==event_id]
        completed=next(g for g in own_grants if g["status"]=="completed")
        event_rows.append({"policy":trial["policy"],"trial":trial["trial"],"event_id":event_id,
            "site":event["site_id"],"base_priority":event["base_priority"],
            "arrival_phase":event["payload"]["arrival_phase"],"has_prerequisite":bool(event["prerequisite_ids"]),
            "created_at":local[event_id]["created_at"],"applied_at":event["applied_at"],
            "created_elapsed_seconds":created-start,"applied_elapsed_seconds":applied-start,
            "capture_to_cloud_seconds":applied-created,"prerequisite_block_seconds":ready-created,
            "cloud_prerequisite_ready_wait_seconds":applied-ready,
            "allocated_priority":completed["priority"],"retry_count":recovery[event_id]["retry_count"],
            "recorded_bypass_count":completed["original_bypass_count"]})

    # Follow sampled durable queue positions to their actual allocation. This
    # measures successful selections, independently of host polling frequency.
    ticket_witness=[]
    seen=set()
    for sample in trial["samples"]:
        sampled=timestamp(sample["sampled_at"])
        for ticket in sample["tickets"]:
            key=(ticket["hospital_id"],ticket["queue_position"])
            if key in seen:
                continue
            seen.add(key)
            candidates=[a for a in allocations if a["hospital_id"]==key[0] and a["original_queue_position"]==key[1]
                        and timestamp(a["created_at"])>=sampled]
            if not candidates:
                continue
            selected=min(candidates,key=lambda a:a["id"])
            others=[a for a in allocations if sampled<=timestamp(a["created_at"])<timestamp(selected["created_at"])]
            ticket_witness.append({"site":key[0],"queue_position":key[1],"observed_at":sample["sampled_at"],
                "allocated_at":selected["created_at"],"other_selections_before_allocation":len(others),
                "observed_predecessors":sum(t["queue_position"]<ticket["queue_position"] for t in sample["tickets"]),
                "observed_bypass_count":ticket["bypass_count"]})

    site_rows=[]
    total_time=max(e["applied_elapsed_seconds"] for e in event_rows)
    simultaneous=sum(all(s["pending_cloud"][site]>0 for site in SITES) for s in trial["samples"] if s["elapsed_seconds"]<=sustained_seconds)
    samples=sum(s["elapsed_seconds"]<=sustained_seconds for s in trial["samples"])
    for site in SITES:
        own=[e for e in event_rows if e["site"]==site]
        sustain=[e for e in own if e["applied_elapsed_seconds"]<=sustained_seconds]
        waits=[e["capture_to_cloud_seconds"] for e in own]
        ready_waits=[e["cloud_prerequisite_ready_wait_seconds"] for e in own]
        own_times=sorted(e["applied_elapsed_seconds"] for e in sustain)
        boundaries=[0]+own_times+[sustained_seconds]
        peer_streak=0
        max_peer_streak=0
        # Cloud-prerequisite readiness does not assert local retry eligibility.
        for other in sorted(event_rows,key=lambda e:(e["applied_elapsed_seconds"],e["event_id"])):
            t=other["applied_elapsed_seconds"]
            eligible=[e for e in own if e["created_elapsed_seconds"]<=t<e["applied_elapsed_seconds"] and
                      e["applied_elapsed_seconds"]-e["cloud_prerequisite_ready_wait_seconds"]<=t]
            if other["site"]==site or not eligible:
                peer_streak=0
            else:
                peer_streak+=1
                max_peer_streak=max(max_peer_streak,peer_streak)
        row={"policy":trial["policy"],"trial":trial["trial"],"site":site,"produced":len(own),"completed":len(own),
             "sustained_completions":len(sustain),"sustained_completion_rps":len(sustain)/sustained_seconds,
             "total_completion_rps":len(own)/total_time,"end_to_end_wait_mean_s":statistics.fmean(waits),
             "end_to_end_wait_p95_s":p95(waits),"end_to_end_wait_max_s":max(waits),
             "prerequisite_ready_wait_p95_s":p95(ready_waits),"prerequisite_ready_wait_max_s":max(ready_waits),
             "max_completion_gap_during_sustained_s":max(b-a for a,b in zip(boundaries,boundaries[1:])),
             "max_peer_completions_while_cloud_prerequisite_ready":max_peer_streak,
             "events_waiting_at_least_600s":sum(w>=600 for w in waits),
             "sustained_completion_fraction":len(sustain)/sum(e["created_elapsed_seconds"]<=sustained_seconds for e in own),
             "backlog_at_sustained_end":trial["sustained_end"]["pending_cloud"][site],
             "final_backlog":0,"both_backlogged_sample_fraction":simultaneous/samples,
             "last_cloud_completion_elapsed_s":max(e["applied_elapsed_seconds"] for e in own)}
        site_rows.append(row)
    return event_rows,site_rows,ticket_witness


def plot_progress(trials,seconds,path):
    fig,axes=plt.subplots(2,3,figsize=(13,7),sharex=True,sharey=True,constrained_layout=True)
    for row,policy in enumerate(("fixed","aimd")):
        for col,n in enumerate((1,2,3)):
            ax=axes[row,col]
            trial=next(t for t in trials if t["policy"]==policy and t["trial"]==n)
            for site in SITES:
                ax.step([s["elapsed_seconds"] for s in trial["samples"]],
                        [s["completed"][site] for s in trial["samples"]],where="post",color=COLORS[site],label=site.replace("_"," "))
            ax.axvline(seconds,color="#666666",ls="--",lw=1,label="Arrivals stop" if row==col==0 else None)
            ax.set_title(f"{policy.upper()} trial {n}")
            ax.grid(alpha=.2)
            if row==1: ax.set_xlabel("Elapsed recovery time (s)")
            if col==0: ax.set_ylabel("Cumulative Cloud completions")
    axes[0,0].legend(fontsize=8)
    fig.suptitle("Hospital A/B progress during 660 s of sustained recovery arrivals and final drain")
    fig.savefig(path,dpi=180)
    plt.close(fig)


def plot_backlog(trials,seconds,path):
    fig,axes=plt.subplots(2,3,figsize=(13,7),sharex=True,sharey=True,constrained_layout=True)
    for row,policy in enumerate(("fixed","aimd")):
        for col,n in enumerate((1,2,3)):
            ax=axes[row,col]
            trial=next(t for t in trials if t["policy"]==policy and t["trial"]==n)
            for site in SITES:
                ax.step([s["elapsed_seconds"] for s in trial["samples"]],
                        [s["pending_cloud"][site] for s in trial["samples"]],where="post",color=COLORS[site],label=site.replace("_"," "))
            for boundary in (seconds/3,2*seconds/3,seconds): ax.axvline(boundary,color="#777777",ls="--",lw=.8)
            ax.set_title(f"{policy.upper()} trial {n}")
            ax.grid(alpha=.2)
            if row==1: ax.set_xlabel("Elapsed recovery time (s)")
            if col==0: ax.set_ylabel("Captured events awaiting Cloud application")
    axes[0,0].legend(fontsize=8)
    fig.suptitle("Durable backlog progression; priority phases change at 220/440 s, arrivals end at 660 s")
    fig.savefig(path,dpi=180)
    plt.close(fig)


def plot_waits(events,path):
    fig,axes=plt.subplots(1,2,figsize=(11,4.6),sharex=True,sharey=True,constrained_layout=True)
    for ax,policy in zip(axes,("fixed","aimd")):
        for site in SITES:
            values=sorted(e["capture_to_cloud_seconds"] for e in events if e["policy"]==policy and e["site"]==site)
            ax.step(values,[(i+1)/len(values) for i in range(len(values))],where="post",color=COLORS[site],label=site.replace("_"," "))
        ax.set_title(policy.upper())
        ax.set_xlabel("Capture-to-Cloud application waiting time (s)")
        ax.grid(alpha=.2)
        ax.legend(fontsize=9)
    axes[0].set_ylabel("Empirical cumulative fraction of completed events")
    fig.suptitle("Observed waiting distributions, pooled descriptively across three trials per policy")
    fig.savefig(path,dpi=180)
    plt.close(fig)


def plot_priority(events,path):
    fig,axes=plt.subplots(1,2,figsize=(11,4.8),sharey=True,constrained_layout=True)
    groups=(("Root p1",lambda e:not e["has_prerequisite"]),("Child p1",lambda e:e["has_prerequisite"] and e["base_priority"]==1),
            ("Child p5",lambda e:e["has_prerequisite"] and e["base_priority"]==5))
    for ax,policy in zip(axes,("fixed","aimd")):
        for i,(label,predicate) in enumerate(groups):
            data=[[e["capture_to_cloud_seconds"] for e in events if e["policy"]==policy and e["site"]==site and predicate(e)] for site in SITES]
            bp=ax.boxplot(data,positions=[i-.17,i+.17],widths=.27,patch_artist=True,showfliers=False)
            for box,site in zip(bp["boxes"],SITES): box.set(facecolor=COLORS[site],alpha=.5)
        ax.set_xticks(range(3),[g[0] for g in groups])
        ax.set_title(policy.upper())
        ax.grid(axis="y",alpha=.2)
    axes[0].set_ylabel("Capture-to-Cloud application waiting time (s)")
    handles=[plt.Line2D([0],[0],color=COLORS[s],lw=7,label=s.replace("_"," ")) for s in SITES]
    axes[1].legend(handles=handles,fontsize=9)
    fig.suptitle("Priority and dependency waiting: base-priority-1 roots can inherit urgency from children")
    fig.savefig(path,dpi=180)
    plt.close(fig)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("raw",type=Path)
    parser.add_argument("--outdir",type=Path,required=True)
    args=parser.parse_args()
    raw=json.loads(args.raw.read_text(encoding="utf-8"))
    assert raw["result"]=="passed"
    seconds=raw["design"]["sustained_arrival_seconds"]
    trials=raw["trials"]
    event_rows,site_rows,ticket_rows=[],[],[]
    for trial in trials:
        events,sites,tickets=inspect_trial(trial,seconds)
        event_rows.extend(events);site_rows.extend(sites)
        ticket_rows.extend({"policy":trial["policy"],"trial":trial["trial"],**t} for t in tickets)
    out=args.outdir
    export_csv(out/"cp7b-sustained-fairness-events-20261029.csv",event_rows)
    export_csv(out/"cp7b-sustained-fairness-summary-20261029.csv",site_rows)
    if ticket_rows: export_csv(out/"cp7b-sustained-fairness-ticket-witnesses-20261029.csv",ticket_rows)
    timeline=[]
    for trial in trials:
        for s in trial["samples"]:
            timeline.append({"policy":trial["policy"],"trial":trial["trial"],"elapsed_seconds":s["elapsed_seconds"],
                "sampled_at":s["sampled_at"],"state":s["state"],"rate_rps":s["rate_rps"],"telemetry_fresh":s["telemetry_fresh"],
                **{site+"_"+field:s[field][site] for site in SITES for field in ("produced","completed","pending_cloud")}})
    export_csv(out/"cp7b-sustained-fairness-timeseries-20261029.csv",timeline)
    minute_rows=[]
    for trial in trials:
        for site in SITES:
            own=[e for e in event_rows if e["policy"]==trial["policy"] and e["trial"]==trial["trial"] and e["site"]==site]
            for minute in range(seconds//60):
                count=sum(minute*60<=e["applied_elapsed_seconds"]<(minute+1)*60 for e in own)
                minute_rows.append({"policy":trial["policy"],"trial":trial["trial"],"site":site,
                                    "interval_start_s":minute*60,"interval_end_s":(minute+1)*60,
                                    "completions":count,"completion_rps":count/60})
    export_csv(out/"cp7b-sustained-fairness-minute-progress-20261029.csv",minute_rows)
    metrics=("sustained_completion_rps","end_to_end_wait_mean_s","end_to_end_wait_p95_s","end_to_end_wait_max_s",
             "max_completion_gap_during_sustained_s","max_peer_completions_while_cloud_prerequisite_ready","last_cloud_completion_elapsed_s")
    aggregate={policy:{site:{metric:describe([r[metric] for r in site_rows if r["policy"]==policy and r["site"]==site])
                            for metric in metrics} for site in SITES} for policy in ("fixed","aimd")}
    maximum=max([t["other_selections_before_allocation"] for t in ticket_rows] or [0])
    violations=[t for t in ticket_rows if t["other_selections_before_allocation"]>t["observed_predecessors"]+1]
    stats={"aggregate_by_policy_and_site":aggregate,"event_count":len(event_rows),"trial_count":len(trials),
        "sampled_ticket_witness_count":len(ticket_rows),"max_other_selections_after_ticket_observed":maximum,
        "sampled_selection_bound_violations":violations,
        "all_events_finished":all(s["produced"]==s["completed"] and s["final_backlog"]==0 for s in site_rows),
        "max_event_wait_seconds":max(e["capture_to_cloud_seconds"] for e in event_rows),
        "events_waiting_at_least_600s":sum(e["capture_to_cloud_seconds"]>=600 for e in event_rows),
        "minimum_completions_in_any_full_sustained_minute":min(r["completions"] for r in minute_rows),
        "zero_completion_minute_count":sum(r["completions"]==0 for r in minute_rows),
        "maximum_allocated_effective_priority":max(e["allocated_priority"] for e in event_rows),
        "base_priority_1_roots_allocated_at_priority_5_or_above":sum(not e["has_prerequisite"] and e["allocated_priority"]>=5 for e in event_rows),
        "controller_states_observed":sorted({s["state"] for t in trials for s in t["samples"]}),
        "observed_admission_rate_range_rps":[min(s["rate_rps"] for t in trials for s in t["samples"]),max(s["rate_rps"] for t in trials for s in t["samples"])],
        "priority_descriptive":{policy:{site:{str(priority):describe([e["capture_to_cloud_seconds"] for e in event_rows if
            e["policy"]==policy and e["site"]==site and e["base_priority"]==priority]) for priority in (1,5)} for site in SITES} for policy in ("fixed","aimd")},
        "interpretation":"Finite sustained evidence; no proof of indefinite elapsed-time fairness; pooled event data are descriptive and are not independent trials."}
    stats["dependency_and_priority_groups"]={}
    for policy in ("fixed","aimd"):
        stats["dependency_and_priority_groups"][policy]={}
        for site in SITES:
            groups={}
            for label,predicate in (("root_priority_1",lambda e:not e["has_prerequisite"]),
                                    ("child_priority_1",lambda e:e["has_prerequisite"] and e["base_priority"]==1),
                                    ("child_priority_5",lambda e:e["has_prerequisite"] and e["base_priority"]==5)):
                values=[e["capture_to_cloud_seconds"] for e in event_rows if e["policy"]==policy and e["site"]==site and predicate(e)]
                groups[label]={**describe(values),"p95":p95(values)}
            stats["dependency_and_priority_groups"][policy][site]=groups
    (out/"cp7b-sustained-fairness-statistics-20261029.json").write_text(json.dumps(stats,indent=2),encoding="utf-8")
    figures=out/"figures"
    figures.mkdir(exist_ok=True)
    plot_progress(trials,seconds,figures/"cp7b-sustained-fairness-completions-20261029.png")
    plot_backlog(trials,seconds,figures/"cp7b-sustained-fairness-backlog-20261029.png")
    plot_waits(event_rows,figures/"cp7b-sustained-fairness-waiting-20261029.png")
    plot_priority(event_rows,figures/"cp7b-sustained-fairness-priority-20261029.png")
    print(json.dumps({k:stats[k] for k in ("trial_count","event_count","all_events_finished","max_event_wait_seconds","events_waiting_at_least_600s","sampled_ticket_witness_count","max_other_selections_after_ticket_observed")},indent=2))


if __name__=="__main__":
    main()
