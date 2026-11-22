"""端到端演示脚本：通过 HTTP 调用运行中的 API（与在 Swagger UI 操作等价）。

用法：python scripts/demo.py [base_url]
"""

from __future__ import annotations

import json
import sys

import httpx

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8000"


def show(title: str, resp: httpx.Response) -> dict:
    print(f"\n===== {title} -> HTTP {resp.status_code} =====")
    try:
        print(json.dumps(resp.json(), ensure_ascii=False, indent=2))
        return resp.json()
    except Exception:
        print(resp.text)
        return {}


def reg(no: str, a: str, b: str) -> dict:
    return {"action": "register", "report_no": no, "paper_a": a, "paper_b": b}


def wd(no: str) -> dict:
    return {"action": "withdraw", "report_no": no}


def main() -> None:
    c = httpx.Client(base_url=BASE, timeout=10)

    show("健康检查", c.get("/health"))

    # 1. 指定基准卷 BASE
    show("批次 B0：指定基准卷 BASE", c.post("/batches", json={
        "batch_no": "B0",
        "baselines": ["BASE"],
        "operations": [reg("R-SEED", "BASE", "SEED")],
    }))

    # 2. 单链登记：X-A-BASE
    show("批次 B1：登记单链 X-A-BASE", c.post("/batches", json={
        "batch_no": "B1",
        "operations": [reg("R1", "X", "A"), reg("R2", "A", "BASE")],
    }))
    show("查 X 资格（期望 single_chain，必经 R1/R2）", c.get("/qualifications/X"))

    # 3. 补一条平行边但仍共用 R2 —— 不能通过
    show("批次 B2：X-A 增加 R3，但 A-BASE 仍共用 R2", c.post("/batches", json={
        "batch_no": "B2", "operations": [reg("R3", "X", "A")],
    }))
    show("查 X 资格（期望 single_chain，仅 R2 必经）", c.get("/qualifications/X"))

    # 4. 再补一条独立 A-BASE 报告 R4 —— 两条边不相交链，通过
    show("批次 B3：A-BASE 增加独立报告 R4", c.post("/batches", json={
        "batch_no": "B3", "operations": [reg("R4", "A", "BASE")],
    }))
    show("查 X 资格（期望 qualified，列出两条链单号）", c.get("/qualifications/X"))

    # 5. 撤回 R4 —— 降回单链，回执失去名单含 X
    show("批次 B4：撤回 R4", c.post("/batches", json={
        "batch_no": "B4", "operations": [wd("R4")],
    }))
    show("查 X 资格（期望 single_chain）", c.get("/qualifications/X"))

    # 6. 再撤回 R2 —— 完全断开，缺依据
    show("批次 B5：撤回 R2", c.post("/batches", json={
        "batch_no": "B5", "operations": [wd("R2")],
    }))
    show("查 X 资格（期望 no_evidence）", c.get("/qualifications/X"))

    # 7. 幂等：B5 原样重送
    show("批次 B5 原样重送（期望 replayed=true，版本不变）", c.post("/batches", json={
        "batch_no": "B5", "operations": [wd("R2")],
    }))

    # 8. 同号异文拒收
    show("批次 B5 同号异文（期望 409 batch_conflict）", c.post("/batches", json={
        "batch_no": "B5", "operations": [reg("R9", "X", "A")],
    }))

    # 9. 版本查询与全量名单
    show("查版本 4 的 X 资格", c.get("/qualifications/X", params={"version": 4}))
    show("最近生效版全量资格", c.get("/qualifications"))
    show("批次列表", c.get("/batches"))
    show("按批号取回 B4 回执", c.get("/batches/B4/receipt"))
    show("报告清单（含撤回状态）", c.get("/reports"))

    # ---------- 10. 显式空列表：撤下全部基准卷 ----------
    # 先恢复到有资格的状态，便于观察“失去资格”名单
    show("批次 B6：补回 R2/R4，X 重新通过", c.post("/batches", json={
        "batch_no": "B6", "operations": [reg("R10", "A", "BASE"), reg("R11", "A", "BASE")],
    }))
    show("查 X 资格（期望 qualified）", c.get("/qualifications/X"))

    clear_payload = {
        "batch_no": "B7",
        "baselines": [],
        "operations": [reg("R-CLEAR", "C", "D")],
    }
    cleared = show("批次 B7：baselines=[] 撤下全部基准卷（期望 disqualified 含 X）",
                   c.post("/batches", json=clear_payload))
    if cleared:
        assert cleared["baselines"] == [], cleared
        assert "X" in cleared["disqualified_papers"], cleared
        assert cleared["current_qualified"] == [], cleared
    show("清空后 X 资格（期望 no_evidence）", c.get("/qualifications/X"))
    show("清空后版本 4 的 X 资格（期望仍按 BASE 查询为 qualified）",
         c.get("/qualifications/X", params={"version": 4}))

    replay = show("批次 B7 原样重送（期望 replayed=true，版本不变）",
                  c.post("/batches", json=clear_payload))
    if replay:
        assert replay["replayed"] is True and replay["version"] == cleared["version"], replay

    conflict = c.post("/batches", json={
        "batch_no": "B7", "operations": [reg("R-CLEAR", "C", "D")]})
    show("批次 B7 去掉 baselines 改成省略（期望 409 batch_conflict）", conflict)
    assert conflict.status_code == 409, conflict.text

    carried = show("批次 B8：省略 baselines（期望沿用上一版，即空）", c.post("/batches", json={
        "batch_no": "B8", "operations": [reg("R-CARRY", "E", "F")],
    }))
    if carried:
        assert carried["baselines"] == [], carried
    replay_carried = show("批次 B8 原样重送（期望 replayed=true）", c.post("/batches", json={
        "batch_no": "B8", "operations": [reg("R-CARRY", "E", "F")],
    }))
    if replay_carried:
        assert replay_carried["replayed"] is True, replay_carried
    conflict_empty = c.post("/batches", json={
        "batch_no": "B8", "baselines": [], "operations": [reg("R-CARRY", "E", "F")]})
    show("批次 B8 改成显式 []（期望 409 batch_conflict）", conflict_empty)
    assert conflict_empty.status_code == 409, conflict_empty.text

    show("最终批次列表", c.get("/batches"))


if __name__ == "__main__":
    main()
