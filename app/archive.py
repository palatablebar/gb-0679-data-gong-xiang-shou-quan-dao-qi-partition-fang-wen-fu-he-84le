import csv,io,json
from app.errors import DomainError
def _blocker(run):
    ruling=run.get("ruling")
    if ruling:
        return None if ruling.get("sealable") else (ruling.get("blocked_reason") or "INCOMPLETE_CHECKPOINT")
    seals=run["stages"][-1]
    if seals.get("metrics",{}).get("ready") is False:
        return seals["metrics"].get("blocked_reason") or "UNSEALABLE"
    return None
def build_archive(run):
    blocked=_blocker(run)
    if blocked: raise DomainError("SEAL_BLOCKED","断点依据不全，不能封存或导出证据",409)
    stream=io.StringIO(newline="");writer=csv.writer(stream)
    writer.writerow(["run_id","input_sha256","rule_version","kind","source_lines","reason","evidence"])
    meta=run["manifest"]
    writer.writerow([run["run_id"],meta["input_sha256"],meta["rule_version"],"manifest",json.dumps([]),meta.get("parameters",{}).get("resume",{}).get("job_id",""),json.dumps({k:meta[k] for k in ("input_sha256","rule_version","generated_at","elapsed_ms")},ensure_ascii=False)])
    ruling=run.get("ruling")
    if ruling:
        writer.writerow([run["run_id"],meta["input_sha256"],meta["rule_version"],"ruling",json.dumps([]),ruling["ruling_id"],json.dumps({k:ruling.get(k) for k in ("job_id","checkpoint_id","checkpoint_digest","completed_before","partitions","started_at","failed_at","resumed_at","effective_at","original_grant_id","original_window","original_expired_at_resume","policy","policy_label","decision","business_claim","audit_position","basis_complete","issues","summary","evidence")},ensure_ascii=False)])
    for row in run["stages"][-1]["rows"]:
        writer.writerow([run["run_id"],meta["input_sha256"],meta["rule_version"],"accepted",json.dumps(row.get("_sources",[])),row.get("status",""),json.dumps(row,ensure_ascii=False)])
    for row in run["stages"][-1].get("cumulative_quarantine",[]):
        writer.writerow([run["run_id"],meta["input_sha256"],meta["rule_version"],"quarantine",json.dumps(row.get("_sources",[])),row.get("reason",""),json.dumps(row,ensure_ascii=False)])
    return "﻿"+stream.getvalue()
