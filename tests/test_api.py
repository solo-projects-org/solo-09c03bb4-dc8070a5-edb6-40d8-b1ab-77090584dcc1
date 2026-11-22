"""端到端 API 自测：通过 Swagger 暴露的同一组 HTTP 接口驱动。"""

from __future__ import annotations

import importlib
import os

import pytest


@pytest.fixture()
def client(monkeypatch, tmp_path):
    monkeypatch.setenv("QUAL_DB_PATH", str(tmp_path / "test.db"))
    import app.main as main
    importlib.reload(main)  # 每个测试函数一个全新的库
    from fastapi.testclient import TestClient

    with TestClient(main.app) as c:
        yield c


BASELINE = {"batch_no": "B0", "baselines": ["BASE"], "operations": [
    {"action": "register", "report_no": "R-SEED", "paper_a": "BASE", "paper_b": "SEED"},
    {"action": "register", "report_no": "R-Y0", "paper_a": "Y", "paper_b": "ISOLATED"}
]}


def _reg(report_no: str, a: str, b: str) -> dict:
    return {"action": "register", "report_no": report_no, "paper_a": a, "paper_b": b}


def _wd(report_no: str) -> dict:
    return {"action": "withdraw", "report_no": report_no}


def _submit(client, payload, expected=200):
    resp = client.post("/batches", json=payload)
    assert resp.status_code == expected, resp.text
    return resp.json()


# ---------- 场景 1：共用报告不能算两条链 ----------

def test_shared_report_does_not_qualify(client):
    """菱形图 X-A/B-BASE，两条路径若共用同一单号则不通过；换一条独立报告后通过。"""
    _submit(client, BASELINE)

    # 只有一条链 X -> A -> BASE
    r1 = _submit(client, {"batch_no": "B1", "operations": [
        _reg("R1", "X", "A"), _reg("R2", "A", "BASE")]})
    assert "X" not in r1["current_qualified"]
    q = client.get("/qualifications/X").json()
    assert q["status"] == "single_chain"
    # 两张报告都是桥，全部列为必经单号
    assert set(q["mandatory_report_nos"]) == {"R1", "R2"}

    # 补一条“看似第二条路”，但 R2 被两条路径共用 -> 仍不通过
    r2 = _submit(client, {"batch_no": "B2", "operations": [_reg("R3", "X", "A")]})
    assert "X" not in r2["current_qualified"]
    q = client.get("/qualifications/X").json()
    assert q["status"] == "single_chain"
    assert set(q["mandatory_report_nos"]) == {"R2"}

    # 再补一条独立的 A->BASE 报告（平行边），两条链不再共用报告 -> 通过
    r3 = _submit(client, {"batch_no": "B3", "operations": [_reg("R4", "A", "BASE")]})
    assert "X" in r3["qualified_papers"]
    q = client.get("/qualifications/X").json()
    assert q["status"] == "qualified"
    assert len(q["chains"]) == 2
    used = [no for chain in q["chains"] for no in chain]
    assert len(used) == len(set(used))  # 两条链无共用报告
    assert set(used) == {"R1", "R2", "R3", "R4"}


def test_same_pair_parallel_reports_count_separately(client):
    """同一对卷型的不同报告分别计入，两条平行边即满足资格。"""
    _submit(client, BASELINE)
    r = _submit(client, {"batch_no": "B1", "operations": [
        _reg("P1", "BASE", "Y"), _reg("P2", "Y", "BASE")]})
    assert "Y" in r["qualified_papers"]
    q = client.get("/qualifications/Y").json()
    assert q["status"] == "qualified" and len(q["chains"]) == 2


# ---------- 场景 2：撤回后降为单链并出现在回执失去名单 ----------

def test_withdraw_downgrades_to_single_chain(client):
    _submit(client, BASELINE)
    _submit(client, {"batch_no": "B1", "operations": [
        _reg("R1", "X", "A"), _reg("R2", "A", "BASE"),
        _reg("R3", "X", "A"), _reg("R4", "A", "BASE")]})
    assert client.get("/qualifications/X").json()["status"] == "qualified"

    receipt = _submit(client, {"batch_no": "B2", "operations": [_wd("R4")]})
    assert "X" in receipt["disqualified_papers"]
    assert "X" not in receipt["current_qualified"]
    q = client.get("/qualifications/X").json()
    assert q["status"] == "single_chain"
    assert set(q["mandatory_report_nos"]) == {"R2"}

    # 再撤回唯一桥边 -> 完全断开，缺依据
    receipt = _submit(client, {"batch_no": "B3", "operations": [_wd("R2")]})
    q = client.get("/qualifications/X").json()
    assert q["status"] == "no_evidence"
    assert q["chains"] == [] and q["mandatory_report_nos"] == []


def test_withdraw_then_reregister_recovers(client):
    _submit(client, BASELINE)
    _submit(client, {"batch_no": "B1", "operations": [
        _reg("P1", "BASE", "Y"), _reg("P2", "Y", "BASE")]})
    _submit(client, {"batch_no": "B2", "operations": [_wd("P2")]})
    assert client.get("/qualifications/Y").json()["status"] == "single_chain"

    receipt = _submit(client, {"batch_no": "B3", "operations": [_reg("P3", "Y", "BASE")]})
    assert "Y" in receipt["qualified_papers"]
    assert client.get("/qualifications/Y").json()["status"] == "qualified"


# ---------- 场景 3：幂等重放 / 同号异文拒收 ----------

def test_replay_returns_same_receipt_and_conflict_rejected(client):
    _submit(client, BASELINE)
    payload = {"batch_no": "B1", "operations": [
        _reg("P1", "BASE", "Y"), _reg("P2", "Y", "BASE")]}
    first = _submit(client, payload)
    assert first["replayed"] is False

    replay = _submit(client, payload)
    assert replay["replayed"] is True
    assert replay["version"] == first["version"]
    assert replay["qualified_papers"] == first["qualified_papers"]
    assert replay["effective_at"] == first["effective_at"]

    # 同号异文 -> 409，且不产生新版本
    bad = client.post("/batches", json={
        "batch_no": "B1",
        "operations": [_reg("P1", "BASE", "Y"), _reg("P9", "Y", "BASE")]})
    assert bad.status_code == 409
    assert bad.json()["error"]["code"] == "batch_conflict"
    versions = client.get("/batches").json()
    assert [v["version"] for v in versions] == [1, 2]


# ---------- 场景 4：版本查询、基准卷与错误处理 ----------

def test_versioned_queries_and_baseline(client):
    _submit(client, BASELINE)
    _submit(client, {"batch_no": "B1", "operations": [
        _reg("P1", "BASE", "Y"), _reg("P2", "Y", "BASE")]})

    q_v1 = client.get("/qualifications/Y", params={"version": 1}).json()
    assert q_v1["status"] == "no_evidence"  # 版本 1 只有种子报告
    q_latest = client.get("/qualifications/Y").json()
    assert q_latest["status"] == "qualified"

    base = client.get("/qualifications/BASE").json()
    assert base["status"] == "baseline"

    missing_version = client.get("/qualifications/Y", params={"version": 99})
    assert missing_version.status_code == 404
    assert missing_version.json()["error"]["code"] == "version_not_found"
    missing_paper = client.get("/qualifications/NOPE")
    assert missing_paper.status_code == 404
    assert missing_paper.json()["error"]["code"] == "paper_not_found"


def test_validation_errors(client):
    _submit(client, BASELINE)

    # 撤回不存在的报告
    r = client.post("/batches", json={"batch_no": "E1", "operations": [_wd("NOPE")]})
    assert r.status_code == 404 and r.json()["error"]["code"] == "report_not_found"

    # 重复登记同一单号
    _submit(client, {"batch_no": "E2", "operations": [_reg("R1", "BASE", "Z")]})
    r = client.post("/batches", json={"batch_no": "E3", "operations": [_reg("R1", "BASE", "Z")]})
    assert r.status_code == 409 and r.json()["error"]["code"] == "report_exists"

    # 自连报告
    r = client.post("/batches", json={"batch_no": "E4", "operations": [_reg("R2", "Z", "Z")]})
    assert r.status_code == 422 and r.json()["error"]["code"] == "self_loop_report"

    # 重复撤回
    _submit(client, {"batch_no": "E5", "operations": [_wd("R1")]})
    r = client.post("/batches", json={"batch_no": "E6", "operations": [_wd("R1")]})
    assert r.status_code == 409 and r.json()["error"]["code"] == "report_inactive"

    # 空操作列表
    r = client.post("/batches", json={"batch_no": "E7", "operations": []})
    assert r.status_code == 422


def test_receipt_lists_gained_and_lost(client):
    _submit(client, BASELINE)
    r1 = _submit(client, {"batch_no": "B1", "operations": [
        _reg("P1", "BASE", "Y"), _reg("P2", "Y", "BASE")]})
    assert r1["qualified_papers"] == ["Y"] and r1["disqualified_papers"] == []

    r2 = _submit(client, {"batch_no": "B2", "operations": [_wd("P2")]})
    assert r2["qualified_papers"] == [] and r2["disqualified_papers"] == ["Y"]

    # 回执可按批号取回
    fetched = client.get("/batches/B2/receipt").json()
    assert fetched["disqualified_papers"] == ["Y"]


def test_baseline_change_recomputes_qualification(client):
    """后续批次指定新的基准卷全集后，资格按新基准重算。"""
    _submit(client, BASELINE)
    _submit(client, {"batch_no": "B1", "operations": [
        _reg("R1", "BASE", "Y"), _reg("R2", "Y", "BASE"),
        _reg("R3", "Y", "Z"), _reg("R4", "Z", "Y")]})
    # Z 经 Y 双连到 BASE -> qualified
    assert client.get("/qualifications/Z").json()["status"] == "qualified"

    # 基准卷换成与 Z 断开的 BASE2：Z/Y 都缺依据
    r = _submit(client, {"batch_no": "B2", "baselines": ["BASE2"], "operations": [
        _reg("R5", "BASE2", "W")]})
    assert r["baselines"] == ["BASE2"]
    assert "Z" in r["disqualified_papers"] and "Y" in r["disqualified_papers"]
    assert client.get("/qualifications/Z").json()["status"] == "no_evidence"
    assert client.get("/qualifications/BASE").json()["status"] == "no_evidence"
    assert client.get("/qualifications/BASE2").json()["status"] == "baseline"


def test_duplicate_baseline_rejected(client):
    r = client.post("/batches", json={
        "batch_no": "D1", "baselines": ["BASE", "BASE"],
        "operations": [_reg("R1", "BASE", "Y")]})
    assert r.status_code == 422 and r.json()["error"]["code"] == "duplicate_baseline"


def test_reports_are_bidirectional(client):
    """报告无方向：从任一端卷型出发都能沿该单号接通。"""
    _submit(client, BASELINE)
    _submit(client, {"batch_no": "B1", "operations": [
        _reg("R1", "BASE", "M"), _reg("R2", "N", "M"),
        _reg("R3", "BASE", "M"), _reg("R4", "M", "N")]})
    q = client.get("/qualifications/N").json()
    assert q["status"] == "qualified"
    # 链中单号无方向，全部来自已登记报告
    for chain in q["chains"]:
        for no in chain:
            assert no in {"R1", "R2", "R3", "R4"}
