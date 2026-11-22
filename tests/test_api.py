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


# ---------- 场景 5：显式空列表撤下全部基准卷 ----------

def _seed_two_chain_graph(client) -> None:
    _submit(client, {"batch_no": "B0", "baselines": ["BASE"], "operations": [
        _reg("R1", "BASE", "Y"), _reg("R2", "Y", "BASE"),
        _reg("R3", "Y", "Z"), _reg("R4", "Z", "Y")]})


def test_empty_baselines_clears_all_and_recomputes(client):
    """baselines=[] 必须真正撤下基准卷，而不是被当成“省略”沿用上一版。"""
    _seed_two_chain_graph(client)
    assert client.get("/qualifications/Y").json()["status"] == "qualified"
    assert client.get("/qualifications/Z").json()["status"] == "qualified"

    receipt = _submit(client, {"batch_no": "B1", "baselines": [], "operations": [
        _reg("R5", "A", "B")]})
    assert receipt["baselines"] == []
    assert receipt["current_qualified"] == []
    # 回执列明相对上一版失去资格的卷型
    assert set(receipt["disqualified_papers"]) == {"Y", "Z"}
    # 原基准卷本身也降为缺依据，并在本批涉及卷型的明细里
    assert receipt["current_status"]["BASE"]["status"] == "no_evidence"
    assert receipt["current_status"]["Y"]["status"] == "no_evidence"
    assert receipt["current_status"]["Z"]["status"] == "no_evidence"

    latest = client.get("/qualifications").json()
    assert latest["baselines"] == []
    assert all(p["status"] == "no_evidence" for p in latest["papers"])

    # 清空前的版本仍按当时的基准卷查询
    v1 = client.get("/qualifications", params={"version": 1}).json()
    assert v1["baselines"] == ["BASE"]
    v1_status = {p["paper_id"]: p["status"] for p in v1["papers"]}
    assert v1_status["BASE"] == "baseline"
    assert v1_status["Y"] == "qualified" and v1_status["Z"] == "qualified"
    assert client.get("/qualifications/BASE", params={"version": 1}).json()["status"] == "baseline"
    assert client.get("/qualifications/BASE").json()["status"] == "no_evidence"


def test_omitted_baselines_keep_effective_previous_version(client):
    """省略 baselines 时沿用实际生效的上一版；上一版清空后也保持为空。"""
    _seed_two_chain_graph(client)

    carried = _submit(client, {"batch_no": "B1", "operations": [_reg("R5", "A", "B")]})
    assert carried["baselines"] == ["BASE"]
    assert client.get("/qualifications/Y").json()["status"] == "qualified"

    cleared = _submit(client, {"batch_no": "B2", "baselines": [], "operations": [
        _reg("R6", "C", "D")]})
    assert cleared["baselines"] == []

    still_empty = _submit(client, {"batch_no": "B3", "operations": [_reg("R7", "E", "F")]})
    assert still_empty["baselines"] == []
    assert still_empty["disqualified_papers"] == []  # 已无资格可失去


def test_clear_and_omitted_are_distinct_for_replay_and_conflict(client):
    """空列表与省略是两种载荷：各自原样重送取原回执，互相改动返回 409。"""
    _seed_two_chain_graph(client)

    clear_payload = {"batch_no": "B1", "baselines": [], "operations": [_reg("R5", "A", "B")]}
    first_clear = _submit(client, clear_payload)
    replay_clear = _submit(client, clear_payload)
    assert replay_clear["replayed"] is True
    assert replay_clear["version"] == first_clear["version"]
    assert replay_clear["disqualified_papers"] == first_clear["disqualified_papers"]

    # 同号改成省略 -> 409，且不新增版本
    omit_same = client.post("/batches", json={
        "batch_no": "B1", "operations": [_reg("R5", "A", "B")]})
    assert omit_same.status_code == 409
    assert omit_same.json()["error"]["code"] == "batch_conflict"
    versions = client.get("/batches").json()
    assert [v["version"] for v in versions] == [1, 2]

    # 反向：先省略（沿用上一版已清空的 []），再改成显式列 BASE -> 409
    omit_payload = {"batch_no": "B2", "operations": [_reg("R6", "C", "D")]}
    first_omit = _submit(client, omit_payload)
    assert first_omit["baselines"] == []
    replay_omit = _submit(client, omit_payload)
    assert replay_omit["replayed"] is True and replay_omit["version"] == first_omit["version"]

    clear_same = client.post("/batches", json={
        "batch_no": "B2", "baselines": ["BASE"], "operations": [_reg("R6", "C", "D")]})
    assert clear_same.status_code == 409
    assert clear_same.json()["error"]["code"] == "batch_conflict"
    versions = client.get("/batches").json()
    assert [v["version"] for v in versions] == [1, 2, 3]


def test_clear_replay_and_conflict_after_restart(tmp_path, monkeypatch):
    """重启后仍按落库载荷核对：重送取原回执，空列表/省略互换仍 409。"""
    import importlib

    import app.main as main
    from fastapi.testclient import TestClient

    db_path = str(tmp_path / "restart.db")
    monkeypatch.setenv("QUAL_DB_PATH", db_path)
    importlib.reload(main)

    clear_payload = {"batch_no": "B1", "baselines": [], "operations": [_reg("R5", "A", "B")]}
    with TestClient(main.app) as c:
        _submit(c, {"batch_no": "B0", "baselines": ["BASE"], "operations": [
            _reg("R1", "BASE", "Y"), _reg("R2", "Y", "BASE")]})
        first = _submit(c, clear_payload)
        versions_before = c.get("/batches").json()

    importlib.reload(main)
    with TestClient(main.app) as c:
        replay = _submit(c, clear_payload)
        assert replay["replayed"] is True
        assert replay["version"] == first["version"]
        assert replay["disqualified_papers"] == ["Y"]

        conflict = c.post("/batches", json={
            "batch_no": "B1", "operations": [_reg("R5", "A", "B")]})
        assert conflict.status_code == 409
        assert conflict.json()["error"]["code"] == "batch_conflict"
        assert c.get("/batches").json() == versions_before


def test_failed_batch_leaves_no_version_or_partial_write(client):
    """批次校验失败时整批回滚：不新增版本，也不留下部分报告。"""
    _submit(client, BASELINE)
    before = client.get("/batches").json()
    # 第一条登记合法，第二条撤回不存在 -> 整批失败
    r = client.post("/batches", json={"batch_no": "F1", "operations": [
        _reg("R-PART", "BASE", "Y"), _wd("NOPE")]})
    assert r.status_code == 404 and r.json()["error"]["code"] == "report_not_found"
    assert client.get("/batches").json() == before
    assert client.get("/reports/R-PART").status_code == 404


def test_historical_version_keeps_edges_after_later_withdrawals(client):
    """清空前的版本按各自基准卷查询；后续撤回也不能改写历史版本的图。"""
    _submit(client, {"batch_no": "B0", "baselines": ["BASE"], "operations": [
        _reg("R1", "BASE", "Y"), _reg("R2", "Y", "BASE")]})
    assert client.get("/qualifications/Y", params={"version": 1}).json()["status"] == "qualified"

    # 同一批里清空基准卷并撤回一条历史报告
    receipt = _submit(client, {"batch_no": "B1", "baselines": [], "operations": [_wd("R2")]})
    assert receipt["baselines"] == []
    assert "Y" in receipt["disqualified_papers"]

    # 版本 1 仍应看到 R2，维持当时的 qualified
    v1 = client.get("/qualifications/Y", params={"version": 1}).json()
    assert v1["status"] == "qualified"
    assert len(v1["chains"]) == 2
    latest = client.get("/qualifications/Y").json()
    assert latest["status"] == "no_evidence"


def test_legacy_payload_accepts_both_clear_and_omit_replay(client):
    """旧库载荷无版本标记、分不出省略与 []：其他内容一致时两种重送都取原回执。"""
    import json
    import sqlite3

    _seed_two_chain_graph(client)
    payload = {"batch_no": "B1", "baselines": [], "operations": [_reg("R5", "A", "B")]}
    first = _submit(client, payload)

    # 把该批载荷改造成修复前的旧格式（没有 payload_schema 标记）
    db_path = os.environ["QUAL_DB_PATH"]
    con = sqlite3.connect(db_path)
    try:
        raw = con.execute(
            "SELECT payload_json FROM versions WHERE batch_no = 'B1'").fetchone()[0]
        legacy = json.loads(raw)
        legacy.pop("payload_schema")
        con.execute(
            "UPDATE versions SET payload_json = ? WHERE batch_no = 'B1'",
            (json.dumps(legacy, ensure_ascii=False, sort_keys=True),))
        con.commit()
    finally:
        con.close()

    # 旧库里 baselines 落成 []，省略重送也按 [] 核对 -> 原回执
    replay_omit = _submit(client, {"batch_no": "B1", "operations": [_reg("R5", "A", "B")]})
    assert replay_omit["replayed"] is True and replay_omit["version"] == first["version"]

    replay_clear = _submit(client, payload)
    assert replay_clear["replayed"] is True and replay_clear["version"] == first["version"]

    # 其他内容不一致仍然拒收
    conflict = client.post("/batches", json={
        "batch_no": "B1", "operations": [_reg("R9", "A", "B")]})
    assert conflict.status_code == 409
    assert [v["version"] for v in client.get("/batches").json()] == [1, 2]
