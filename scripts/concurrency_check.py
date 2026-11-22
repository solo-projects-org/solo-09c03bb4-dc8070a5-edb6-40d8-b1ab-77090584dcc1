"""并发批次核对：N 个批次同时提交，验证生效顺序串行、版本连续无丢失。"""

from __future__ import annotations

import json
import sys
import threading

import httpx

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8000"


def main() -> None:
    n = 12
    errors: list[str] = []
    results: list[dict] = []
    lock = threading.Lock()

    def submit(i: int) -> None:
        payload = {
            "batch_no": f"CC-{i:03d}",
            "operations": [
                {"action": "register", "report_no": f"CC-R{i:03d}-1",
                 "paper_a": f"P{i % 4}", "paper_b": f"P{(i + 1) % 4}"},
                {"action": "register", "report_no": f"CC-R{i:03d}-2",
                 "paper_a": f"P{(i + 2) % 4}", "paper_b": f"P{(i + 3) % 4}"},
            ],
        }
        try:
            r = httpx.post(f"{BASE}/batches", json=payload, timeout=30)
            with lock:
                if r.status_code != 200:
                    errors.append(f"CC-{i:03d} HTTP {r.status_code}: {r.text}")
                else:
                    results.append(r.json())
        except Exception as exc:  # noqa: BLE001
            with lock:
                errors.append(f"CC-{i:03d} exception: {exc}")

    threads = [threading.Thread(target=submit, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    print(f"提交 {n} 个并发批次，成功 {len(results)} 个，错误 {len(errors)} 个")
    for e in errors:
        print("ERROR:", e)

    versions = sorted(r["version"] for r in results)
    print("生效版本号序列:", versions)
    assert len(results) == n, "有批次未成功生效"
    assert versions == list(range(versions[0], versions[0] + n)), "版本号不连续"
    assert len({r["batch_no"] for r in results}) == n, "批次号重复"

    # 全部报告应可查且为 active
    reports = httpx.get(f"{BASE}/reports", timeout=30).json()
    cc_reports = [r for r in reports if r["report_no"].startswith("CC-")]
    assert len(cc_reports) == 2 * n, f"报告数不符: {len(cc_reports)}"
    assert all(r["status"] == "active" for r in cc_reports)
    reg_versions = sorted(r["reg_version"] for r in cc_reports)
    print(f"报告登记版本覆盖 {reg_versions[0]}..{reg_versions[-1]}，共 {len(cc_reports)} 份全部 active")

    # 同一并发批次再全部重放：应返回原回执，不新增版本
    replay_versions_before = max(versions)
    replay_ok = 0
    for i in range(n):
        payload = {
            "batch_no": f"CC-{i:03d}",
            "operations": [
                {"action": "register", "report_no": f"CC-R{i:03d}-1",
                 "paper_a": f"P{i % 4}", "paper_b": f"P{(i + 1) % 4}"},
                {"action": "register", "report_no": f"CC-R{i:03d}-2",
                 "paper_a": f"P{(i + 2) % 4}", "paper_b": f"P{(i + 3) % 4}"},
            ],
        }
        r = httpx.post(f"{BASE}/batches", json=payload, timeout=30)
        body = r.json()
        if r.status_code == 200 and body["replayed"] is True:
            replay_ok += 1
    batches_after = httpx.get(f"{BASE}/batches", timeout=30).json()
    assert max(b["version"] for b in batches_after) == replay_versions_before, "重放产生了新版本"
    print(f"并发批次原样重放 {replay_ok}/{n} 返回 replayed=true，版本仍止于 {replay_versions_before}")
    print("CONCURRENCY CHECK PASSED")


if __name__ == "__main__":
    main()
