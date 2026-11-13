"""Preserve partial evidence and remove exact interrupted fairness test projects."""
import argparse
import importlib.util
import json
from pathlib import Path

HERE=Path(__file__).resolve().parent
ROOT=HERE.parents[1]
spec=importlib.util.spec_from_file_location("harness",HERE/"cp7b-fixed-vs-aimd-experiment-20261029.py")
base=importlib.util.module_from_spec(spec)
spec.loader.exec_module(base)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--work",type=Path,required=True)
    parser.add_argument("--output",type=Path,required=True)
    args=parser.parse_args()
    work=args.work.resolve()
    assert work.parent==(ROOT/".venv").resolve() and work.name.startswith("cp7b-fairness-")
    assert not args.output.exists()
    projects=[]
    for folder in work.iterdir():
        if folder.is_dir() and folder.name.startswith("cg-fair-") and (folder/"compose.json").exists():
            config=json.loads((folder/"compose.json").read_text(encoding="utf-8"))
            assert config["name"]==folder.name
            projects.append((folder,config))
    assert len(projects)==6
    runner=base.Runner(work,"unused",args.output,1)
    before,volumes_before=runner.inventory()
    report={"result":"interrupted_not_a_completed_experiment",
        "reason":"Stopped after source review found final duplicate check expected 200 for a terminal completed grant; the correct fenced response is 409. Sustained partial measurements are retained but excluded from final claims.",
        "trials":[],"cleanup_started_at":base.utcnow()}
    disposable_ids=set()
    disposable_volumes=set()
    for folder,config in projects:
        name=folder.name
        ids=runner.docker("ps","-aq","--filter","label=com.docker.compose.project="+name).splitlines()
        inspected=json.loads(runner.docker("inspect",*ids))
        assert len(inspected)==7 and all(r["Config"]["Labels"]["com.docker.compose.project"]==name for r in inspected)
        disposable_ids.update(r["Id"] for r in inspected)
        volumes=runner.docker("volume","ls","-q","--filter","label=com.docker.compose.project="+name).splitlines()
        assert len(volumes)==3 and all(v.startswith(name+"_") for v in volumes)
        disposable_volumes.update(volumes)
        partial=json.loads((folder/"raw.json").read_text(encoding="utf-8"))
        partial["result"]="interrupted"
        report["trials"].append(partial)
        runner.compose_call(name,folder/"compose.json","down","--remove-orphans")
        for volume in volumes:
            metadata=json.loads(runner.docker("volume","inspect",volume))[0]
            assert metadata["Labels"]["com.docker.compose.project"]==name
            runner.docker("volume","rm",volume)
        for resource in ("volume","network"):
            assert not runner.docker(resource,"ls","-q","--filter","label=com.docker.compose.project="+name)
        assert not runner.docker("ps","-aq","--filter","label=com.docker.compose.project="+name)
        (folder/"compose.json").unlink()
    after,volumes_after=runner.inventory()
    originals={key:value for key,value in before.items() if key not in disposable_ids}
    assert originals==after and volumes_before-disposable_volumes==volumes_after
    report.update(cleanup="passed",existing_resources_preserved=True,
        original_containers=len(originals),original_volumes=len(volumes_after),finished_at=base.utcnow())
    args.output.write_text(json.dumps(report,indent=2),encoding="utf-8")
    print(f"Cleaned {len(projects)} exact disposable projects; preserved {len(originals)} existing containers and {len(volumes_after)} volumes")


if __name__=="__main__":
    main()
