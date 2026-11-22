"""FastAPI 入口：跨校成绩入仓资格核查 API。"""

from __future__ import annotations

import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, Query
from fastapi.responses import JSONResponse

from .db import Database
from .schemas import BatchReceipt, BatchSubmitRequest, Qualification, ReportView
from .service import ApiError, QualificationService

DB_PATH = os.environ.get("QUAL_DB_PATH", "/data/qualification.db")


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.db = Database(DB_PATH)
    app.state.service = QualificationService(app.state.db)
    yield


app = FastAPI(
    title="跨校成绩入仓资格核查 API",
    description=(
        "教研员按批号登记/撤回等值报告；每个生效批次产生一个版本，"
        "卷型须有两条不共用报告的等值链通往基准卷才取得入仓资格。"
    ),
    version="1.0.0",
    lifespan=lifespan,
)


@app.exception_handler(ApiError)
async def api_error_handler(_request, exc: ApiError):
    return JSONResponse(
        status_code=exc.status_code,
        content={"error": {"code": exc.code, "message": exc.message}},
    )


@app.get("/health", tags=["系统"], summary="健康检查")
def health():
    return {"status": "ok"}


@app.post(
    "/batches",
    tags=["批次"],
    summary="提交批次（登记/撤回报告，可同时指定基准卷全集）",
    response_model=BatchReceipt,
)
def submit_batch(req: BatchSubmitRequest):
    return app.state.service.submit_batch(req)


@app.get("/batches", tags=["批次"], summary="列出所有生效批次/版本")
def list_batches():
    return app.state.service.list_versions()


@app.get(
    "/batches/{batch_no}/receipt",
    tags=["批次"],
    summary="按批号查回执（已生效批次原样重送也返回原回执）",
    response_model=BatchReceipt,
)
def get_batch_receipt(batch_no: str):
    return app.state.service.get_receipt(batch_no)


@app.get(
    "/qualifications/{paper_id}",
    tags=["资格"],
    summary="查单个卷型资格（默认最近生效版）",
    response_model=Qualification,
)
def get_qualification(paper_id: str, version: int | None = Query(None, ge=1, description="生效版号，缺省取最近版")):
    return app.state.service.get_qualification(paper_id, version)


@app.get("/qualifications", tags=["资格"], summary="查全部卷型资格（同一生效版）")
def list_qualifications(version: int | None = Query(None, ge=1, description="生效版号，缺省取最近版")):
    return app.state.service.list_qualifications(version)


@app.get("/reports", tags=["报告"], summary="列出全部报告（含已撤回）", response_model=list[ReportView])
def list_reports():
    return app.state.service.list_reports()


@app.get("/reports/{report_no}", tags=["报告"], summary="按单号查报告", response_model=ReportView)
def get_report(report_no: str):
    return app.state.service.get_report(report_no)
