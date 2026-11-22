"""业务服务层：批次生效、资格重算、回执生成、幂等重放。"""

from __future__ import annotations

import json
from typing import Any

from . import graph
from .db import Database
from .schemas import (
    BatchReceipt,
    BatchSubmitRequest,
    Qualification,
    ReportRegister,
    ReportView,
    ReportWithdraw,
)


class ApiError(Exception):
    def __init__(self, status_code: int, code: str, message: str):
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message


def _parse_operations(operations: list[dict]) -> list[ReportRegister | ReportWithdraw]:
    parsed: list[ReportRegister | ReportWithdraw] = []
    for idx, raw in enumerate(operations):
        if not isinstance(raw, dict):
            raise ApiError(422, "invalid_operation", f"第 {idx + 1} 条操作必须是对象")
        action = raw.get("action")
        try:
            if action == "register":
                parsed.append(ReportRegister(**raw))
            elif action == "withdraw":
                parsed.append(ReportWithdraw(**raw))
            else:
                raise ApiError(422, "invalid_operation", f"第 {idx + 1} 条操作的 action 非法: {action!r}")
        except ApiError:
            raise
        except Exception as exc:  # pydantic 校验错误
            raise ApiError(422, "invalid_operation", f"第 {idx + 1} 条操作格式有误: {exc}") from exc
    return parsed


def canonical_payload(req: BatchSubmitRequest, operations: list) -> str:
    """归一化载荷：已生效批次原样重送据此判定（同号异文拒收）。"""
    payload = {
        "batch_no": req.batch_no,
        "baselines": sorted(set(req.baselines)),
        "operations": [op.model_dump() for op in operations],
    }
    return json.dumps(payload, ensure_ascii=False, sort_keys=True)


class QualificationService:
    def __init__(self, db: Database):
        self.db = db

    # ---------- 资格计算 ----------

    def _compute_all(self, conn, version: int) -> dict[str, dict]:
        edges = self.db.active_edges_at(conn, version)
        baselines = self.db.baselines_at(conn, version)
        papers = self.db.all_papers_at(conn, version)
        result: dict[str, dict] = {}
        for paper in sorted(papers):
            outcome = graph.analyze(paper, edges, baselines)
            if outcome is not None:
                result[paper] = outcome
        return result

    @staticmethod
    def _qualified_set(outcomes: dict[str, dict]) -> set[str]:
        return {p for p, o in outcomes.items() if o["status"] == "qualified"}

    # ---------- 批次提交 ----------

    def submit_batch(self, req: BatchSubmitRequest) -> dict[str, Any]:
        operations = _parse_operations(req.operations)
        canonical = canonical_payload(req, operations)

        with self.db.write_transaction() as conn:
            existing = conn.execute(
                "SELECT v.payload_json, r.receipt_json FROM versions v"
                " LEFT JOIN receipts r ON r.batch_no = v.batch_no"
                " WHERE v.batch_no = ?",
                (req.batch_no,),
            ).fetchone()

            if existing is not None:
                if existing["payload_json"] != canonical:
                    raise ApiError(409, "batch_conflict",
                                   f"批次号 {req.batch_no} 已生效且载荷不同（同号异文拒收）")
                receipt = json.loads(existing["receipt_json"])
                receipt["replayed"] = True
                return receipt

            # 批次内操作自身校验
            seen: set[str] = set()
            if req.baselines and len(set(req.baselines)) != len(req.baselines):
                raise ApiError(422, "duplicate_baseline", "基准卷列表存在重复项")
            for op in operations:
                if op.report_no in seen:
                    raise ApiError(422, "duplicate_in_batch",
                                   f"批次内报告单号重复: {op.report_no}")
                seen.add(op.report_no)

                if isinstance(op, ReportRegister):
                    if op.paper_a == op.paper_b:
                        raise ApiError(422, "self_loop_report",
                                       f"报告 {op.report_no} 不能连接同一个卷型")
                    row = self.db.get_report(conn, op.report_no)
                    if row is not None:
                        raise ApiError(409, "report_exists",
                                       f"报告单号 {op.report_no} 已存在（状态: {row['status']}）")
                else:
                    row = self.db.get_report(conn, op.report_no)
                    if row is None:
                        raise ApiError(404, "report_not_found",
                                       f"报告单号 {op.report_no} 不存在，无法撤回")
                    if row["status"] != "active":
                        raise ApiError(409, "report_inactive",
                                       f"报告单号 {op.report_no} 已撤回，不能重复撤回")

            prev_version = self.db.latest_version(conn)
            prev_outcomes = self._compute_all(conn, prev_version) if prev_version > 0 else {}
            prev_qualified = self._qualified_set(prev_outcomes)

            prev_baselines = self.db.baselines_at(conn, prev_version) if prev_version > 0 else []
            new_baselines = sorted(set(req.baselines)) if req.baselines else list(prev_baselines)

            new_version = prev_version + 1
            self.db.insert_version(conn, new_version, req.batch_no,
                                   json.loads(canonical), new_baselines)
            if req.baselines:
                self.db.insert_baseline_event(conn, new_version, req.batch_no, new_baselines)

            touched_papers: set[str] = set(new_baselines)
            for op in operations:
                if isinstance(op, ReportRegister):
                    self.db.insert_report_event(
                        conn, new_version, req.batch_no,
                        op.report_no, op.paper_a, op.paper_b, "register")
                    self.db.register_report(
                        conn, op.report_no, op.paper_a, op.paper_b,
                        req.batch_no, new_version)
                    touched_papers.update({op.paper_a, op.paper_b})
                else:
                    row = self.db.get_report(conn, op.report_no)
                    self.db.insert_report_event(
                        conn, new_version, req.batch_no,
                        op.report_no, row["paper_a"], row["paper_b"], "withdraw")
                    self.db.withdraw_report(conn, op.report_no, req.batch_no, new_version)
                    touched_papers.update({row["paper_a"], row["paper_b"]})

            new_outcomes = self._compute_all(conn, new_version)
            new_qualified = self._qualified_set(new_outcomes)

            gained = sorted(new_qualified - prev_qualified)
            lost = sorted(prev_qualified - new_qualified)

            detail_papers = touched_papers | set(gained) | set(lost)
            current_status = {
                paper: Qualification(paper_id=paper, version=new_version, **new_outcomes[paper]).model_dump()
                for paper in sorted(detail_papers)
                if paper in new_outcomes
            }

            version_row = self.db.get_version_row(conn, new_version)
            receipt = BatchReceipt(
                batch_no=req.batch_no,
                version=new_version,
                effective_at=version_row["effective_at"],
                baselines=new_baselines,
                qualified_papers=gained,
                disqualified_papers=lost,
                current_qualified=sorted(new_qualified),
                current_status=current_status,
                replayed=False,
            ).model_dump()

            self.db.insert_receipt(conn, new_version, req.batch_no, receipt)
            return receipt

    # ---------- 查询 ----------

    def _resolve_version(self, conn, version: int | None) -> int:
        latest = self.db.latest_version(conn)
        if latest == 0:
            raise ApiError(404, "no_version", "尚无任何生效批次")
        if version is None:
            return latest
        if version <= 0 or version > latest:
            raise ApiError(404, "version_not_found", f"版本 {version} 不存在，最近生效版为 {latest}")
        return version

    def get_qualification(self, paper_id: str, version: int | None) -> dict:
        with self.db.read_connection() as conn:
            version = self._resolve_version(conn, version)
            edges = self.db.active_edges_at(conn, version)
            baselines = self.db.baselines_at(conn, version)
            outcome = graph.analyze(paper_id, edges, baselines)
            if outcome is None:
                raise ApiError(404, "paper_not_found",
                               f"卷型 {paper_id} 在版本 {version} 中没有任何报告或基准记录")
            return Qualification(paper_id=paper_id, version=version, **outcome).model_dump()

    def list_qualifications(self, version: int | None) -> dict:
        with self.db.read_connection() as conn:
            version = self._resolve_version(conn, version)
            outcomes = self._compute_all(conn, version)
            return {
                "version": version,
                "baselines": self.db.baselines_at(conn, version),
                "papers": [
                    Qualification(paper_id=paper, version=version, **outcome).model_dump()
                    for paper, outcome in sorted(outcomes.items())
                ],
            }

    def list_reports(self) -> list[dict]:
        with self.db.read_connection() as conn:
            return [dict(r) for r in self.db.list_reports(conn)]

    def get_report(self, report_no: str) -> dict:
        with self.db.read_connection() as conn:
            row = self.db.get_report(conn, report_no)
            if row is None:
                raise ApiError(404, "report_not_found", f"报告单号 {report_no} 不存在")
            return dict(row)

    def list_versions(self) -> list[dict]:
        with self.db.read_connection() as conn:
            rows = self.db.list_versions(conn)
            return [
                {"version": r["version"], "batch_no": r["batch_no"],
                 "effective_at": r["effective_at"],
                 "baselines": json.loads(r["baselines_json"])}
                for r in rows
            ]

    def get_receipt(self, batch_no: str) -> dict:
        with self.db.read_connection() as conn:
            row = self.db.get_receipt(conn, batch_no)
            if row is None:
                raise ApiError(404, "batch_not_found", f"批次 {batch_no} 未生效或不存在")
            receipt = json.loads(row["receipt_json"])
            receipt["replayed"] = False
            return receipt
