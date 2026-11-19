"""续跑批次的唯一到期裁定。

裁决：续跑视为一次新的访问，授权必须按续跑时刻（resumed_at）重新匹配。
原作业的授权不随作业延续，到期时刻立即失效。业务方“同一作业沿用原授权”
的主张只作为证据保留，不作为放行依据。

裁定按 (job_id, checkpoint_id, checkpoint_digest) 唯一确定。同一断点重试
只会复用已有裁定，不会新增；中断的续跑不会覆盖旧版本与原始证据。
断点依据不全的批次仍可复核，但不得封存/导出证据。
"""

import hashlib
import json
import re
from copy import deepcopy
from datetime import datetime, timezone

from app.errors import DomainError
from app.values import instant

POLICY = "resume_reauthorize"
POLICY_LABEL = "按续跑时刻重取授权"
DECISION = "续跑按新访问处理，以 resumed_at 重新匹配授权；原授权到期即失效，不随作业延续。"
BUSINESS_CLAIM = "同一作业，应沿用原授权"
AUDIT_POSITION = "每次续跑视作新访问，授权到期后不得放行"

_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_PARTITION = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")

_TIME_FIELDS = (
    ("started_at", "RESUME_STARTED_AT"),
    ("failed_at", "RESUME_FAILED_AT"),
    ("resumed_at", "RESUME_RESUMED_AT"),
)


def _text(value):
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _parse_time(value):
    try:
        return instant(value)
    except (DomainError, TypeError, ValueError):
        return None


def default_grants():
    from app.modules.m3_grants.rules import GRANTS

    return GRANTS


def effective_grants(params):
    grants = params.get("grants")
    return grants if isinstance(grants, list) else default_grants()


def checkpoint_key(job_id, checkpoint_id, checkpoint_digest):
    material = "\x1f".join([job_id, checkpoint_id, checkpoint_digest])
    return hashlib.sha256(material.encode()).hexdigest()


def _grant_hash(grants):
    encoded = json.dumps(grants, ensure_ascii=False, sort_keys=True, default=str).encode()
    return hashlib.sha256(encoded).hexdigest()


def prepare(params, raw):
    """读取并规范化 parameters.resume，返回裁定上下文（没有续跑时返回 None）。

    会将规范化后的 resume 写回 params，使同一断点的重试得到稳定 digest。
    """
    from app import store

    source = params.get("resume")
    if source is None:
        return None
    if not isinstance(source, dict):
        raise DomainError("BAD_RESUME", "续跑断点需要对象")

    spec = deepcopy(source)
    params["resume"] = spec
    issues = []

    job_id = _text(spec.get("job_id"))
    checkpoint_id = _text(spec.get("checkpoint_id"))
    digest = _text(spec.get("checkpoint_digest"))
    digest = digest.lower() if digest else None
    if not job_id:
        issues.append("RESUME_JOB_ID_MISSING")
    if not checkpoint_id:
        issues.append("RESUME_CHECKPOINT_ID_MISSING")
    if not digest:
        issues.append("RESUME_CHECKPOINT_DIGEST_MISSING")
    elif not _HEX64.match(digest):
        issues.append("RESUME_CHECKPOINT_DIGEST_INVALID")

    key = checkpoint_key(job_id, checkpoint_id, digest) if job_id and checkpoint_id and digest else None
    existing = store.find_ruling(key) if key else None
    if existing and existing.get("resumed_at"):
        # 同一断点的重试沿用首次裁定的续跑时刻，重试时间不得改变授权基准。
        spec["resumed_at"] = existing["resumed_at"]

    completed = spec.get("completed_before")
    if completed is None:
        issues.append("RESUME_COMPLETED_BEFORE_MISSING")
    elif isinstance(completed, bool) or not isinstance(completed, int) or completed < 0:
        issues.append("RESUME_COMPLETED_BEFORE_INVALID")

    partitions = spec.get("partitions")
    if not isinstance(partitions, list) or not partitions:
        issues.append("RESUME_PARTITIONS_MISSING")
    else:
        normalized = []
        bad = False
        for part in partitions:
            if not isinstance(part, str) or not _PARTITION.match(part):
                bad = True
                continue
            normalized.append(part)
        if bad:
            issues.append("RESUME_PARTITIONS_INVALID")
        else:
            spec["partitions"] = sorted(set(normalized))

    parsed = {}
    for field, code in _TIME_FIELDS:
        value = spec.get(field)
        if value is None or value == "":
            issues.append(code + "_MISSING")
            continue
        moment = _parse_time(value)
        if moment is None:
            issues.append(code + "_INVALID")
            continue
        spec[field] = moment.isoformat()
        parsed[field] = moment
    if len(parsed) == len(_TIME_FIELDS) and not (
        parsed["started_at"] <= parsed["failed_at"] <= parsed["resumed_at"]
    ):
        issues.append("RESUME_TIME_ORDER_INVALID")

    grants = effective_grants(params)
    grant_sha = _grant_hash(grants)
    grant_id = _text(spec.get("original_grant_id"))
    original_window = None
    expired_at_resume = None
    if not grant_id:
        issues.append("RESUME_ORIGINAL_GRANT_MISSING")
    else:
        grant = next((item for item in grants if isinstance(item, dict) and item.get("id") == grant_id), None)
        if grant is None:
            issues.append("RESUME_ORIGINAL_GRANT_NOT_FOUND")
        else:
            grant_start = _parse_time(grant.get("start"))
            grant_end = _parse_time(grant.get("end"))
            if grant_start is None or grant_end is None:
                issues.append("RESUME_ORIGINAL_GRANT_INVALID")
            else:
                original_window = {"start": grant_start.isoformat(), "end": grant_end.isoformat()}
                resume_at = parsed.get("resumed_at")
                expired_at_resume = bool(resume_at and resume_at >= grant_end)

    input_sha = hashlib.sha256(raw).hexdigest()
    conflict = False
    if existing:
        prior = existing.get("evidence", {})
        mismatch = False
        if prior.get("input_sha256") and prior["input_sha256"] != input_sha:
            mismatch = True
        if prior.get("grant_sha256") and prior["grant_sha256"] != grant_sha:
            mismatch = True
        if existing.get("partitions") is not None and existing.get("partitions") != spec.get("partitions"):
            mismatch = True
        if existing.get("completed_before") is not None and existing.get("completed_before") != completed:
            mismatch = True
        if mismatch:
            conflict = True
            issues.append("CHECKPOINT_EVIDENCE_MISMATCH")

    resumed_at = parsed.get("resumed_at")
    return {
        "job_id": job_id,
        "checkpoint_id": checkpoint_id,
        "checkpoint_digest": digest,
        "checkpoint_key": key,
        "ruling_id": ("R-" + key[:20]) if key else None,
        "completed_before": completed,
        "partitions": spec.get("partitions") if isinstance(spec.get("partitions"), list) else None,
        "started_at": spec.get("started_at") if "started_at" in parsed else None,
        "failed_at": spec.get("failed_at") if "failed_at" in parsed else None,
        "resumed_at": resumed_at.isoformat() if resumed_at else None,
        "effective_at": resumed_at.isoformat() if resumed_at else None,
        "original_grant_id": grant_id,
        "original_window": original_window,
        "original_expired_at_resume": expired_at_resume,
        "grant_sha256": grant_sha,
        "input_sha256": input_sha,
        "basis_complete": not issues,
        "issues": issues,
        "uncovered_partitions": [],
        "conflict": conflict,
        "reused": bool(existing),
        "policy": POLICY,
    }


def check_partition_coverage(spec, rows):
    """续跑行必须落在断点声明的分区集合内，否则断点依据不全。"""
    if not spec:
        return
    declared = set(spec.get("partitions") or [])
    uncovered = sorted(
        {
            row.get("partition")
            for row in rows
            if row.get("partition") and declared and row.get("partition") not in declared
        }
    )
    if uncovered:
        spec["uncovered_partitions"] = uncovered
        if "RESUME_PARTITION_NOT_COVERED" not in spec["issues"]:
            spec["issues"].append("RESUME_PARTITION_NOT_COVERED")
        spec["basis_complete"] = False


def blocked_reason(spec):
    if not spec:
        return None
    if not spec.get("basis_complete"):
        return "INCOMPLETE_CHECKPOINT"
    if spec.get("conflict"):
        return "CHECKPOINT_EVIDENCE_MISMATCH"
    if not spec.get("ruling_id"):
        return "INCOMPLETE_CHECKPOINT"
    return None


def finalize(spec, result):
    """在流水线结束后形成可按断点唯一的到期裁定。"""
    if not spec:
        return None

    rows = result["stages"][-1]["rows"]
    manifest = result["manifest"]
    resumed = [row for row in rows if row.get("resume_evaluated")]
    summary = {
        "resumed": len(resumed),
        "allowed": sum(row.get("expected") == "1" for row in resumed),
        "denied": sum(row.get("expected") == "0" for row in resumed),
        "violations": sum(row.get("violation") == "越权放行" for row in resumed),
        "false_blocks": sum(row.get("violation") == "误拦截" for row in resumed),
    }
    evidence = {
        "input_sha256": manifest["input_sha256"],
        "grant_sha256": spec["grant_sha256"],
        "rule_version": manifest["rule_version"],
        "rows_sha256": hashlib.sha256(
            json.dumps(rows, ensure_ascii=False, sort_keys=True, default=str).encode()
        ).hexdigest(),
        "row_count": len(rows),
        "resumed_rows": len(resumed),
        "summary": summary,
    }

    reason = blocked_reason(spec)
    return {
        "ruling_id": spec.get("ruling_id"),
        "checkpoint_key": spec.get("checkpoint_key"),
        "job_id": spec.get("job_id"),
        "checkpoint_id": spec.get("checkpoint_id"),
        "checkpoint_digest": spec.get("checkpoint_digest"),
        "completed_before": spec.get("completed_before"),
        "partitions": spec.get("partitions"),
        "started_at": spec.get("started_at"),
        "failed_at": spec.get("failed_at"),
        "resumed_at": spec.get("resumed_at"),
        "effective_at": spec.get("effective_at"),
        "original_grant_id": spec.get("original_grant_id"),
        "original_window": spec.get("original_window"),
        "original_expired_at_resume": spec.get("original_expired_at_resume"),
        "policy": POLICY,
        "policy_label": POLICY_LABEL,
        "decision": DECISION,
        "business_claim": BUSINESS_CLAIM,
        "audit_position": AUDIT_POSITION,
        "basis_complete": bool(spec.get("basis_complete")),
        "issues": list(spec.get("issues", [])),
        "uncovered_partitions": list(spec.get("uncovered_partitions", [])),
        "conflict": bool(spec.get("conflict")),
        "reused": bool(spec.get("reused")),
        "sealable": reason is None,
        "blocked_reason": reason,
        "evidence": evidence,
        "summary": summary,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }


def seal_blocked(ruling):
    """返回证据导出的阻断原因；可封存时返回 None。"""
    if not ruling:
        return None
    if ruling.get("sealable"):
        return None
    return ruling.get("blocked_reason") or "INCOMPLETE_CHECKPOINT"
