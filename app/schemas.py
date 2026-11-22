"""请求/响应模型。"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator

Status = Literal["baseline", "qualified", "single_chain", "no_evidence"]


class ReportRegister(BaseModel):
    model_config = {"extra": "forbid"}

    action: Literal["register"]
    report_no: str = Field(min_length=1, description="报告唯一单号")
    paper_a: str = Field(min_length=1, description="卷型甲")
    paper_b: str = Field(min_length=1, description="卷型乙")

    @field_validator("report_no", "paper_a", "paper_b")
    @classmethod
    def _strip(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("不能为空字符串")
        return v


class ReportWithdraw(BaseModel):
    model_config = {"extra": "forbid"}

    action: Literal["withdraw"]
    report_no: str = Field(min_length=1, description="要撤回的报告单号")

    @field_validator("report_no")
    @classmethod
    def _strip(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("不能为空字符串")
        return v


class BatchSubmitRequest(BaseModel):
    model_config = {"extra": "forbid"}

    batch_no: str = Field(min_length=1, description="批次号")
    baselines: list[str] = Field(default_factory=list, description="本批生效的基准卷全集（不提供则沿用）")
    operations: list[dict] = Field(min_length=1, description="登记/撤回操作列表")

    @field_validator("batch_no")
    @classmethod
    def _strip_batch(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("不能为空字符串")
        return v

    @field_validator("baselines")
    @classmethod
    def _strip_baselines(cls, v: list[str]) -> list[str]:
        cleaned = [b.strip() for b in v]
        if any(not b for b in cleaned):
            raise ValueError("基准卷不能为空字符串")
        return cleaned


class Qualification(BaseModel):
    paper_id: str
    version: int
    status: Status
    label: str
    chains: list[list[str]] = Field(description="通过时：两条链各自的报告单号")
    mandatory_report_nos: list[str] = Field(description="单链时：所有必经报告单号")


class BatchReceipt(BaseModel):
    batch_no: str
    version: int
    effective_at: str
    baselines: list[str]
    qualified_papers: list[str] = Field(description="本批后取得资格的卷型（相对上一版新增）")
    disqualified_papers: list[str] = Field(description="本批后失去资格的卷型（相对上一版移除）")
    current_qualified: list[str] = Field(description="本批生效后全部有资格的非基准卷")
    current_status: dict[str, Qualification] = Field(description="本批涉及卷型的当前资格明细")
    replayed: bool = Field(description="是否为已生效批次的原样重送")


class ReportView(BaseModel):
    report_no: str
    paper_a: str
    paper_b: str
    status: Literal["active", "withdrawn"]
    reg_batch: str
    reg_version: int
    wd_batch: str | None
    wd_version: int | None
