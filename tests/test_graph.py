"""图算法单元测试：边不相交双连通、桥、平行边、环。"""

from app.graph import analyze


def test_disconnected_is_no_evidence():
    # X 自成一连通分量，基准卷 BASE 在另一分量
    r = analyze("X", [("R1", "X", "Q"), ("R0", "BASE", "B")], ["BASE"])
    assert r is not None and r["status"] == "no_evidence"


def test_single_chain_reports_all_bridges():
    edges = [("R1", "X", "A"), ("R2", "A", "BASE"), ("R3", "X", "C"), ("R4", "C", "A")]
    r = analyze("X", edges, ["BASE"])
    assert r["status"] == "single_chain"
    # X-A-BASE 与 X-C-A-BASE：R2 是唯一桥
    assert r["mandatory_report_nos"] == ["R2"]
    assert len(r["chains"]) == 1 and r["chains"][0][-1] == "R2"


def test_two_vertex_independent_paths_with_shared_node_ok():
    # 两条链经过相同卷型 A，但不共用报告（平行边）-> 通过
    edges = [
        ("R1", "X", "A"), ("R2", "X", "A"),
        ("R3", "A", "BASE"), ("R4", "A", "BASE"),
    ]
    r = analyze("X", edges, ["BASE"])
    assert r["status"] == "qualified"
    flat = [no for c in r["chains"] for no in c]
    assert len(flat) == len(set(flat)) == 4


def test_shared_edge_fails_even_with_extra_branch():
    # X 到 A 有两条边，但 A->BASE 只有一张报告，共用 -> 单链，R3 必经
    edges = [("R1", "X", "A"), ("R2", "X", "A"), ("R3", "A", "BASE")]
    r = analyze("X", edges, ["BASE"])
    assert r["status"] == "single_chain"
    assert r["mandatory_report_nos"] == ["R3"]


def test_two_baselines_fully_disjoint():
    edges = [("R1", "X", "B1"), ("R2", "X", "B2")]
    r = analyze("X", edges, ["B1", "B2"])
    assert r["status"] == "qualified"
    assert sorted(sum(r["chains"], [])) == ["R1", "R2"]


def test_cycle_then_shared_bridge():
    # 环 X-A-C-X 提供局部冗余，但出环节点到基准只有一张报告
    edges = [
        ("R1", "X", "A"), ("R2", "A", "C"), ("R3", "C", "X"),
        ("R4", "A", "BASE"),
    ]
    r = analyze("X", edges, ["BASE"])
    assert r["status"] == "single_chain"
    assert r["mandatory_report_nos"] == ["R4"]


def test_baseline_paper_is_baseline_status():
    r = analyze("BASE", [("R1", "BASE", "X")], ["BASE"])
    assert r["status"] == "baseline"


def test_unknown_paper_returns_none():
    assert analyze("GHOST", [("R1", "BASE", "X")], ["BASE"]) is None


def test_two_chains_may_share_intermediate_node():
    # X->A 两条平行边，A->BASE 两条平行边：链在 A 汇合但不共用报告
    edges = [
        ("E1", "X", "A"), ("E2", "X", "A"),
        ("E3", "A", "BASE"), ("E4", "A", "BASE"),
    ]
    r = analyze("X", edges, ["BASE"])
    assert r["status"] == "qualified"
    chains = {tuple(sorted(c)) for c in r["chains"]}
    assert chains == {("E1", "E3"), ("E2", "E4")} or chains == {("E1", "E4"), ("E2", "E3")}
