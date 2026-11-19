from app.values import instant,number
from app.reasons import reject
from app.errors import DomainError
from app import rulings
GRANTS=[{"id":"G-01","principal":"analyst","resource":"orders","start":"2026-11-01T00:00:00Z","end":"2026-12-01T00:00:00Z","from_partition":"2026-01","to_partition":"2026-12","effect":"allow"},{"id":"G-02","principal":"analyst","resource":"orders","start":"2026-11-10T00:00:00Z","end":"2026-11-20T00:00:00Z","from_partition":"2026-10","to_partition":"2026-11","effect":"deny"},{"id":"G-03","principal":"auditor","resource":"customers","start":"2026-11-01T00:00:00Z","end":"2026-12-01T00:00:00Z","from_partition":"2026-01","to_partition":"2026-12","effect":"allow"}]
def validate_grants(grants):
    ids=set()
    for g in grants:
        if g["id"] in ids or instant(g["start"])>=instant(g["end"]) or g["effect"] not in {"allow","deny"} or g["from_partition"]>=g["to_partition"]:raise DomainError("BAD_GRANT","授权版本无效")
        ids.add(g["id"])
    return grants
def evaluate(rows,context):
    params=context.get("parameters",{})
    grants=validate_grants(rulings.effective_grants(params))
    resume=context.get("resume")
    resume_basis=resume.get("resumed_at") if resume else None
    good=[];bad=[]
    for row in rows:
        # 续跑批次整体按续跑时刻重新匹配授权；原授权时间窗不随作业延续。
        resumed=bool(resume)
        try:
            at=instant(row["access_time"]);reported=number(row["reported"])
            if reported not in {0,1}:raise DomainError("BAD_OBSERVED","报送判定必须为0或1")
            if resumed:
                if not resume_basis: raise DomainError("INCOMPLETE_CHECKPOINT","续跑时刻缺失，不能按新访问重取授权")
                at=instant(resume_basis)
            active=[g for g in grants if g["principal"]==row["principal"] and g["resource"]==row["resource"] and instant(g["start"])<=at<instant(g["end"])]
            extra={"at":at.isoformat(),"candidates":active,"candidate_count":len(active),"reported":str(reported)}
            if resumed:
                extra.update({"resume_evaluated":True,"resume_basis":"resumed_at","resume_ruling_id":resume.get("ruling_id"),"original_access_time":row["access_time"],"original_grant_id":resume.get("original_grant_id"),"original_expired_at_resume":resume.get("original_expired_at_resume")})
            good.append({**row,**extra})
        except (DomainError,KeyError) as e:bad.append(reject(row,getattr(e,"code","MISSING_FIELD"),str(e)))
    return {"rows":good,"quarantine":bad,"metrics":{"active_hits":sum(r["candidate_count"] for r in good),"grant_versions":len(grants),"resume_rows":sum(bool(r.get("resume_evaluated")) for r in good)}}
