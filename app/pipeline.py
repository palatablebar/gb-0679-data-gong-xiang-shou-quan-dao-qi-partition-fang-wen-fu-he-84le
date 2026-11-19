import time
from app.config import ROOT,MODULES
from app.errors import DomainError
from app.values import scalar
from app.manifest import digest,manifest
from app import store,rulings
from app.modules.m1_accesses.rules import evaluate as stage1
from app.modules.m2_resources.rules import evaluate as stage2
from app.modules.m3_grants.rules import evaluate as stage3
from app.modules.m4_conflicts.rules import evaluate as stage4
from app.modules.m5_partitions.rules import evaluate as stage5
from app.modules.m6_audit.rules import evaluate as stage6
from app.modules.m7_seals.rules import evaluate as stage7
STAGES=[stage1,stage2,stage3,stage4,stage5,stage6,stage7]
def load_sample(name):
    if name not in {"dirty.csv","clean.csv","legacy.csv"}: raise DomainError("BAD_SAMPLE","样例不存在",404)
    return (ROOT/"sample_data"/name).read_bytes()
def calculate(raw,params=None,stop=6):
    params=params or {};started=time.monotonic()
    resume=rulings.prepare(params,raw)
    context={"parameters":params,"raw":raw,"quarantine":[],"resume":resume};stages=[];rows=[]
    for index,stage in enumerate(STAGES[:stop+1]):
        result=stage(raw if index==0 else rows,context)
        result=scalar(result);rows=result["rows"]
        context["quarantine"].extend(result.get("quarantine",[]))
        if index==0: context["intake"]=result["metrics"]
        result["cumulative_quarantine"]=list(context["quarantine"])
        context[MODULES[index][0]]=result
        stages.append(result)
    if resume is not None:
        scope=stages[1]["rows"] if len(stages)>1 else rows
        rulings.check_partition_coverage(resume,scope)
    payload={"run_id":digest(raw,params)[:20],"stages":stages,"manifest":manifest(raw,params,started),"parameters":params}
    if stop==6:
        ruling=rulings.finalize(resume,payload)
        payload["ruling"]=ruling
        if ruling is not None:
            metrics=payload["stages"][6]["metrics"]
            metrics["resume_ruling"]=ruling["ruling_id"]
            metrics["resume_rows"]=ruling["summary"]["resumed"]
            metrics["resume_denied"]=ruling["summary"]["denied"]
            metrics["basis_complete"]=ruling["basis_complete"]
            metrics["sealable"]=ruling["sealable"]
            metrics["blocked_reason"]=ruling["blocked_reason"]
            if not ruling["sealable"]: metrics["ready"]=False
    return payload
def execute(raw,params=None):
    result=calculate(raw,params)
    if result.get("ruling") is not None:
        attempt=result["ruling"]
        stored=store.record_ruling(result["run_id"],attempt)
        canonical=stored["ruling"]
        # 同一断点只保留首次裁定与原始证据；本次尝试只叠加自己的完整性判断。
        merged={**canonical,
                "conflict":attempt["conflict"] or bool(canonical.get("conflict")),
                "issues":attempt["issues"],"basis_complete":attempt["basis_complete"],
                "uncovered_partitions":attempt["uncovered_partitions"],
                "sealable":attempt["sealable"],"blocked_reason":attempt["blocked_reason"]}
        if not canonical.get("sealable",True):
            # 唯一裁定一旦形成即为准；后续重试不能把原本不全的依据“补齐”成可封存。
            merged["sealable"]=False
            merged["basis_complete"]=False
            merged["blocked_reason"]=canonical.get("blocked_reason") or "INCOMPLETE_CHECKPOINT"
            merged["issues"]=list(dict.fromkeys(list(merged["issues"])+list(canonical.get("issues",[]))))
            if canonical.get("blocked_reason") not in merged["issues"]:
                merged["issues"]=merged["issues"]+[canonical.get("blocked_reason")]
        if merged["conflict"] and not canonical.get("conflict"):
            merged["sealable"]=False
            merged["blocked_reason"]=merged["blocked_reason"] or "CHECKPOINT_EVIDENCE_MISMATCH"
        if attempt["reused"] or stored["run_id"]!=result["run_id"]:
            merged.update({"reused":True,"active_run_id":stored["run_id"]})
        if attempt["evidence"]!=canonical["evidence"]:
            merged["attempt_evidence"]=attempt["evidence"]
        result["ruling"]=merged
        metrics=result["stages"][6]["metrics"]
        metrics["resume_ruling"]=canonical["ruling_id"]
        metrics["basis_complete"]=merged["basis_complete"] and not merged["conflict"]
        metrics["sealable"]=merged["sealable"]
        metrics["blocked_reason"]=merged["blocked_reason"]
        if not merged["sealable"]: metrics["ready"]=False
    saved=store.save(result["run_id"],result)
    if result.get("ruling") is not None: saved["ruling"]=result["ruling"]
    return saved
def stage_preview(index,payload):
    if "rows" in payload and index>0:
        if not isinstance(payload["rows"],list) or any(not isinstance(row,dict) for row in payload["rows"]): raise DomainError("BAD_ROWS","记录需要对象列表")
        if not isinstance(payload.get("context",{}),dict): raise DomainError("BAD_CONTEXT","上下文需要对象")
        return scalar(STAGES[index](payload["rows"],payload.get("context",{"parameters":{},"quarantine":[]})))
    raw=payload.get("csv")
    raw=raw.encode() if isinstance(raw,str) else load_sample(payload.get("sample","dirty.csv"))
    return calculate(raw,payload.get("parameters",{}),index)["stages"][-1]
