import json

import pytest

from app import rulings, store
from app.errors import DomainError
from app.pipeline import calculate, execute

# 业务时间线：原授权 G-02（analyst / orders，2026-11-10 → 2026-11-20）在续跑前到期。
# G-01 从 2026-11-01 起生效到 2026-12-01，用于对照“按续跑时刻重取”。
ROW = """event_id,principal,resource,partition,access_time,observed,reported,note
00001,analyst,orders,2026-10,2026-11-10T12:00:00Z,allowed,1,到期前启动
00002,analyst,orders,2026-10,2026-11-10T12:00:00Z,allowed,1,到期前启动
"""


def frame(resumed_at="2026-11-25T08:00:00Z", checkpoint="CKP-1", job="JOB-9", digest="a" * 64, **extra):
    base = {
        "job_id": job,
        "checkpoint_id": checkpoint,
        "checkpoint_digest": digest,
        "completed_before": 1000,
        "partitions": ["2026-10"],
        "started_at": "2026-11-10T00:00:00Z",
        "failed_at": "2026-11-10T12:00:00Z",
        "resumed_at": resumed_at,
        "original_grant_id": "G-02",
    }
    base.update(extra)
    return base


def run(resume=None, raw=ROW.encode()):
    params = {"resume": resume} if resume is not None else {}
    return execute(raw, params)


def test_ruling_picks_resume_time_not_original_grant():
    payload = run(frame())
    ruling = payload["ruling"]
    assert ruling["policy"] == "resume_reauthorize"
    assert ruling["original_expired_at_resume"] is True
    # 原授权 G-02 在续跑时已到期，永远不能作为放行依据。
    for row in payload["stages"][4]["rows"]:
        assert "G-02" not in row["allow_rules"]
        assert row["at"] == "2026-11-25T08:00:00+00:00"
        assert row["resume_basis"] == "resumed_at"


def test_ruling_is_per_checkpoint_and_reused_on_retry():
    first = run(frame())
    second = run(frame())
    assert first["ruling"]["ruling_id"] == second["ruling"]["ruling_id"]
    assert len(store.rulings()) == 1
    assert second["ruling"]["reused"] is True


def test_retry_cannot_change_resume_moment():
    first = run(frame())
    later = run(frame(resumed_at="2026-12-05T08:00:00Z"))
    rows = later["stages"][4]["rows"]
    assert all(row["at"] == "2026-11-25T08:00:00+00:00" for row in rows)
    assert later["ruling"]["resumed_at"] == first["ruling"]["resumed_at"]


def test_incomplete_checkpoint_cannot_seal():
    payload = run(frame(completed_before=None, checkpoint="CKP-2", digest="b" * 64))
    ruling = payload["ruling"]
    assert ruling["basis_complete"] is False and ruling["sealable"] is False
    assert "RESUME_COMPLETED_BEFORE_MISSING" in ruling["issues"]
    assert payload["stages"][6]["metrics"]["ready"] is False


def test_incomplete_checkpoint_archive_blocked(client):
    spec = frame(completed_before=None, checkpoint="CKP-3", digest="c" * 64)
    created = client.post("/api/runs", json={"csv": ROW, "parameters": {"resume": spec}}).get_json()
    response = client.get("/api/runs/" + created["run_id"] + "/archive")
    assert response.status_code == 409
    assert response.get_json()["code"] == "SEAL_BLOCKED"


def test_complete_checkpoint_archive_has_ruling(client):
    spec = frame(checkpoint="CKP-4", digest="d" * 64)
    created = client.post("/api/runs", json={"csv": ROW, "parameters": {"resume": spec}}).get_json()
    response = client.get("/api/runs/" + created["run_id"] + "/archive")
    assert response.status_code == 200
    body = response.data.decode("utf-8-sig")
    assert "ruling" in body and created["ruling"]["ruling_id"] in body
    assert "resume_reauthorize" in body


def test_checkpoint_other_evidence_conflicts():
    first = run(frame(checkpoint="CKP-5", digest="e" * 64))
    other = """event_id,principal,resource,partition,access_time,observed,reported,note
00009,analyst,orders,2026-10,2026-11-10T12:00:00Z,allowed,1,不同证据
"""
    changed = run(frame(checkpoint="CKP-5", digest="e" * 64), raw=other.encode())
    assert changed["ruling"]["conflict"] is True and changed["ruling"]["sealable"] is False
    assert "CHECKPOINT_EVIDENCE_MISMATCH" in changed["ruling"]["issues"]
    assert len(store.rulings()) == 1


def test_interrupted_resume_keeps_previous_ruling(client):
    spec = frame(checkpoint="CKP-6", digest="f" * 64)
    created = client.post("/api/runs", json={"csv": ROW, "parameters": {"resume": spec}}).get_json()
    before = client.get("/api/runs/" + created["run_id"] + "/ruling").get_json()
    # 续跑中断：再次复核失败不影响旧批次版本与原始证据。
    client.post("/api/runs/" + created["run_id"] + "/review", json={"version": 1, "decision": "已复核"})
    after = client.get("/api/runs/" + created["run_id"] + "/ruling").get_json()
    assert before["ruling_id"] == after["ruling_id"]
    assert before["resumed_at"] == after["resumed_at"]
    assert before["evidence"] == after["evidence"]
    assert store.get(created["run_id"])["version"] == 2


def test_partition_outside_checkpoint_cannot_seal():
    crossed = ROW + "00003,analyst,orders,2026-11,2026-11-10T12:00:00Z,allowed,1,跨分区\n"
    payload = run(frame(checkpoint="CKP-7", digest="1" * 64), raw=crossed.encode())
    ruling = payload["ruling"]
    assert ruling["uncovered_partitions"] == ["2026-11"]
    assert ruling["sealable"] is False


def test_review_page_shows_ruling(client):
    spec = frame(checkpoint="CKP-8", digest="9" * 64)
    client.post("/api/runs", json={"csv": ROW, "parameters": {"resume": spec}})
    page = client.get("/audit").data.decode()
    assert "resume --ruling" in page and "按续跑时刻重取授权" in page


def test_bad_resume_shape_rejected(client):
    assert client.post("/api/runs", json={"csv": ROW, "parameters": {"resume": "x"}}).status_code == 422
