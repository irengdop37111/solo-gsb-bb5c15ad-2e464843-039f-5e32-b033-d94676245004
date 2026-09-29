"""Pydantic 请求/响应模型。"""
from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field

EventType = Literal["transfer", "receipt", "seal_check", "storage"]
SealResult = Literal["intact", "damaged"]


class SampleIn(BaseModel):
    sample_code: str = Field(..., min_length=1, max_length=64, description="样本编号（业务唯一）")
    collected_at: str = Field(..., description="采集时间 ISO-8601，如 2026-09-20T08:00:00Z")
    collector: str = Field(..., min_length=1, max_length=64, description="采集人")
    seal_number: str = Field(..., min_length=1, max_length=64, description="封签号")


class EventIn(BaseModel):
    event_no: str = Field(..., min_length=1, max_length=64, description="唯一事件号，重试请保持不变")
    sample_code: str = Field(..., min_length=1, max_length=64)
    event_type: EventType
    occurred_at: str = Field(..., description="事件发生时间 ISO-8601（不是录入时间，用于重算排序）")
    # transfer
    from_custodian: Optional[str] = Field(None, description="移交人（移交事件必填，须为当前保管人）")
    to_custodian: Optional[str] = Field(None, description="接收人（移交事件必填，签收事件须匹配此人）")
    # receipt
    transfer_no: Optional[str] = Field(None, description="签收所对应的移交事件号（签收事件必填）")
    receiver: Optional[str] = Field(None, description="签收人（签收事件必填）")
    # seal_check
    checker: Optional[str] = Field(None, description="检查人（封签检查事件必填）")
    seal_result: SealResult = Field("intact", description="封签检查结果")
    seal_number: Optional[str] = Field(None, description="封签号（所有事件必填，须与登记封签一致）")
    # storage
    lab: Optional[str] = Field(None, description="入库实验室（入库事件必填）")

    def type_required_fields(self) -> list[str]:
        """返回按事件类型必须提供的字段名列表。"""
        common = ["seal_number"]
        if self.event_type == "transfer":
            return common + ["from_custodian", "to_custodian"]
        if self.event_type == "receipt":
            return common + ["transfer_no", "receiver"]
        if self.event_type == "seal_check":
            return common + ["checker"]
        return common + ["lab"]  # storage


class ApproveIn(BaseModel):
    approver: str = Field(..., min_length=1, max_length=64, description="核定人")
    note: Optional[str] = Field(None, max_length=255)
