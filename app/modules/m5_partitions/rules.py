from app.reasons import reject
from app.values import number
from app.errors import DomainError
def evaluate(rows,context):
    good=[];bad=[]
    for row in rows:
        if row.get("observed") not in {"allowed","blocked"}:bad.append(reject(row,"BAD_ACCESS_OUTCOME","访问结果必须为allowed或blocked"));continue
        effect=row["effect"];expected="1" if effect=="allow" else "0";actual="1" if row["observed"]=="allowed" else "0"
        extra={"expected":expected,"reported":actual,"upstream_decision":row["reported"],"partition_result":"授权内" if row["matched_rules"] else "未授权分区"}
        if row.get("resume_evaluated"):
            extra["resume_basis"]="resumed_at"
            extra["resume_ruling_id"]=row.get("resume_ruling_id")
            extra["original_grant_id"]=row.get("original_grant_id")
        extra["violation"]="越权放行" if expected=="0" and actual=="1" else "误拦截" if expected=="1" and actual=="0" else "一致"
        good.append({**row,**extra})
    return {"rows":good,"quarantine":bad,"metrics":{"allowed":sum(r["expected"]=="1" for r in good),"blocked":sum(r["expected"]=="0" for r in good),"violations":sum(r["violation"]=="越权放行" for r in good),"false_blocks":sum(r["violation"]=="误拦截" for r in good)}}
