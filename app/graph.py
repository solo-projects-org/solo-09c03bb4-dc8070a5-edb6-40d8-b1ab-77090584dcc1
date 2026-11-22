"""卷型等值图的资格算法。

图模型：
- 卷型是节点，报告是连接两个卷型的**无向边**，每条边有唯一单号。
- 同一对卷型可以存在多条平行边（不同单号分别计入）。
- 基准卷构成目标集合。

资格判定：
- baseline（基准卷）：基准卷本身免核查。
- qualified（通过）：从该卷型到基准卷集合存在两条**边不相交**的路径，
  即两条链不能共用同一份报告（节点/卷型可以重复经过）。
- single_chain（未通过-单链）：仍能接通基准卷，但最大边不相交路径数只有一条，
  此时列出所有“必经单号”（删除后该卷型即与基准卷断开的桥边）。
- no_evidence（缺依据）：与基准卷集合完全断开。

实现：把每条无向边建成一对互为反向的单位容量弧（共四条残量弧），
以待查卷型为源点、超级汇点连接所有基准卷，跑 Edmonds-Karp，
最多增广两次即可区分 0 / 1 / 2 条边不相交链。
"""

from __future__ import annotations

from collections import defaultdict, deque
from typing import Iterable

SINK = ("__baseline_sink__",)

STATUS_LABELS = {
    "baseline": "基准卷",
    "qualified": "通过",
    "single_chain": "未通过-单链",
    "no_evidence": "缺依据",
}


def _build_residual(
    edges: list[tuple[str, str, str]],
    baselines: Iterable[str],
) -> tuple[dict, dict, set]:
    """构建残量网络。

    返回 (graph, forward_arcs, nodes)：
    - graph[node] 中的每条弧为 [to, reverse_index, residual_cap, edge_id]；
    - forward_arcs[eid] = (u->v 正向弧引用, v->u 正向弧引用)，初始容量均为 1。
    """
    nodes: set = set(baselines)
    for _eid, u, v in edges:
        nodes.add(u)
        nodes.add(v)

    graph: dict = {node: [] for node in nodes}
    graph[SINK] = []

    def add_arc(frm, to, cap, eid):
        graph[frm].append([to, len(graph[to]), cap, eid])
        graph[to].append([frm, len(graph[frm]) - 1, 0, eid])

    forward_arcs: dict[str, tuple[list, list]] = {}
    for eid, u, v in edges:
        add_arc(u, v, 1, eid)
        arc_uv = graph[u][-1]
        add_arc(v, u, 1, eid)
        arc_vu = graph[v][-1]
        forward_arcs[eid] = (arc_uv, arc_vu)

    # 基准卷 -> 超级汇点。容量给 2，允许两条链终止于同一个基准卷。
    for b in baselines:
        add_arc(b, SINK, 2, None)

    return graph, forward_arcs, nodes


def _augment(graph: dict, source: str) -> bool:
    """BFS 找一条增广路并增广 1 个单位，找到返回 True。"""
    prev: dict = {source: None}
    queue = deque([source])
    while queue and SINK not in prev:
        cur = queue.popleft()
        for idx, arc in enumerate(graph[cur]):
            to, _rev, cap, _eid = arc
            if cap > 0 and to not in prev:
                prev[to] = (cur, idx)
                queue.append(to)
    if SINK not in prev:
        return False

    cur = SINK
    while cur != source:
        frm, idx = prev[cur]
        arc = graph[frm][idx]
        arc[2] -= 1  # 正向残量 -1
        graph[arc[0]][arc[1]][2] += 1  # 反向残量 +1
        cur = frm
    return True


def _decompose_chains(
    source: str,
    flow_arcs: list[tuple[str, str, str]],
    baselines: set,
) -> list[list[str]]:
    """把单位流分解为 source -> 基准卷的链（单号序列），并剔除可能的环。"""
    outgoing: dict[str, list[tuple[str, str]]] = defaultdict(list)
    for u, v, eid in flow_arcs:
        outgoing[u].append((v, eid))

    chains: list[list[str]] = []
    while outgoing[source]:
        route_nodes = [source]
        route_edges: list[str] = []
        position = {source: 0}
        cur = source
        while cur not in baselines:
            nxt, eid = outgoing[cur].pop()
            if nxt in position:
                # 流分解中出现环：把环从路径中裁掉
                cut = position[nxt]
                for n in route_nodes[cut + 1:]:
                    position.pop(n, None)
                del route_nodes[cut + 1:]
                del route_edges[cut:]
                cur = nxt
                continue
            position[nxt] = len(route_nodes)
            route_nodes.append(nxt)
            route_edges.append(eid)
            cur = nxt
        chains.append(route_edges)
    return chains


def _mandatory_edges(
    source: str,
    edges: list[tuple[str, str, str]],
    baselines: set,
) -> list[str]:
    """列出所有必经单号：删除该边后 source 无法到达任何基准卷。"""
    adjacency: dict[str, list[tuple[str, str]]] = defaultdict(list)
    for eid, u, v in edges:
        adjacency[u].append((v, eid))
        adjacency[v].append((u, eid))

    def reaches_baseline(skip_eid: str | None) -> bool:
        seen = {source}
        queue = deque([source])
        while queue:
            cur = queue.popleft()
            if cur in baselines:
                return True
            for nxt, eid in adjacency[cur]:
                if eid == skip_eid or nxt in seen:
                    continue
                seen.add(nxt)
                queue.append(nxt)
        return False

    if not reaches_baseline(None):
        return []
    return [eid for eid, _u, _v in edges if not reaches_baseline(eid)]


def analyze(
    source: str,
    edges: list[tuple[str, str, str]],
    baselines: Iterable[str],
) -> dict | None:
    """判定单个卷型的资格。

    edges: [(report_no, paper_a, paper_b), ...]
    返回 None 表示该卷型在当前版本的图中不存在。
    """
    baseline_set = set(baselines)
    graph, forward_arcs, nodes = _build_residual(edges, baseline_set)

    if source not in nodes:
        return None
    if source in baseline_set:
        return {
            "status": "baseline",
            "label": STATUS_LABELS["baseline"],
            "chains": [],
            "mandatory_report_nos": [],
        }

    flow = 0
    while flow < 2 and _augment(graph, source):
        flow += 1

    if flow == 0:
        return {
            "status": "no_evidence",
            "label": STATUS_LABELS["no_evidence"],
            "chains": [],
            "mandatory_report_nos": [],
        }

    # 由残量还原每条边的净流向：净流 = (1 - 弧uv残量) - (1 - 弧vu残量)
    flow_arcs: list[tuple[str, str, str]] = []
    for eid, u, v in edges:
        arc_uv, arc_vu = forward_arcs[eid]
        net = arc_vu[2] - arc_uv[2]  # 1 表示 u->v，-1 表示 v->u
        if net == 1:
            flow_arcs.append((u, v, eid))
        elif net == -1:
            flow_arcs.append((v, u, eid))

    if flow == 2:
        chains = _decompose_chains(source, flow_arcs, baseline_set)
        return {
            "status": "qualified",
            "label": STATUS_LABELS["qualified"],
            "chains": chains,
            "mandatory_report_nos": [],
        }

    # flow == 1：能接通但只有一条边不相交链，列出全部必经单号
    mandatory = _mandatory_edges(source, edges, baseline_set)
    chains = _decompose_chains(source, flow_arcs, baseline_set)
    return {
        "status": "single_chain",
        "label": STATUS_LABELS["single_chain"],
        "chains": chains,
        "mandatory_report_nos": mandatory,
    }
