"""FastAPI 入口：样本交接链核验 HTTP API。

业务约定：
- POST /api/samples               样本登记
- POST /api/events                移交 / 签收 / 封签检查 / 实验室入库（统一入口，按 event_type 区分）
- GET  /api/samples               样本清单（含结论摘要）
- GET  /api/samples/{code}        证据链、当前保管人、结论与原因
- GET  /api/samples/{code}/events 原始事件（按发生时间排列）
- POST /api/samples/{code}/approve 仅有效链可核定入库；核定后历史锁定
- GET  /api/events/{event_no}     按事件号查询
"""
from __future__ import annotations

import json
from typing import Any, Optional

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from . import database as db
from .config import settings
from .schemas import ApproveIn, EventIn, SampleIn
from .seed import seed_if_empty
from .verification import canonical_ts, event_content_hash, now_ts, verify_sample

app = FastAPI(
    title="样本交接链核验服务",
    description="环境检测机构样本保管链（chain of custody）登记、核验与核定 API",
    version="1.0.0",
)


class ApiError(Exception):
    def __init__(self, status_code: int, code: str, message: str, details: Any = None):
        self.status_code = status_code
        self.code = code
        self.message = message
        self.details = details


@app.exception_handler(ApiError)
async def api_error_handler(_: Request, exc: ApiError) -> JSONResponse:
    body: dict[str, Any] = {"error": {"code": exc.code, "message": exc.message}}
    if exc.details is not None:
        body["error"]["details"] = exc.details
    return JSONResponse(status_code=exc.status_code, content=body)


@app.on_event("startup")
def on_startup() -> None:
    db.init_db()
    if settings.seed_demo:
        seeded = seed_if_empty()
        if seeded:
            print("[seed] 已写入演示样本（设置 COC_SEED_DEMO=0 可关闭）")


# ---------------- 工具 ----------------

def _payload_for_event(e: EventIn) -> dict[str, Any]:
    """归一化业务内容；幂等/冲突比较以该内容为准。"""
    missing = [f for f in e.type_required_fields() if getattr(e, f) in (None, "")]
    if missing:
        raise ApiError(422, "MISSING_FIELDS",
                       f"事件类型 {e.event_type} 缺少必填字段", missing)
    payload = e.model_dump(exclude_none=True)
    # event_no 是句柄不参与内容；统一规范化发生时间
    payload.pop("event_no", None)
    payload["occurred_at"] = canonical_ts(e.occurred_at)
    return payload


def _require_sample_unlocked(sample) -> None:
    if sample and sample["approved"]:
        raise ApiError(409, "HISTORY_LOCKED",
                       "样本已核定入库，历史记录锁定，拒绝改写")


def _summary(row) -> dict[str, Any]:
    events = db.list_events(row["sample_code"])
    r = verify_sample(row, events)
    return {
        "sample_code": r["sample_code"],
        "status": r["status"],
        "current_custodian": r["current_custodian"],
        "in_transit": r["in_transit"],
        "approved": r["approved"],
        "event_count": r["event_count"],
        "has_valid_receipt": r["has_valid_receipt"],
        "has_storage": r["has_storage"],
    }


# ---------------- 健康检查 ----------------

@app.get("/health", tags=["meta"])
def health() -> dict[str, str]:
    return {"status": "ok"}


# ---------------- 样本登记 ----------------

@app.post("/api/samples", tags=["samples"], status_code=201)
def create_sample(body: SampleIn) -> dict[str, Any]:
    collected = canonical_ts(body.collected_at)
    with db.write_tx() as conn:
        existing = conn.execute(
            "SELECT * FROM samples WHERE sample_code=?", (body.sample_code,)
        ).fetchone()
        if existing:
            same = (
                existing["collected_at"] == collected
                and existing["collector"] == body.collector
                and existing["seal_number"] == body.seal_number
            )
            if same:
                # 同内容重试幂等
                return JSONResponse(status_code=200, content={
                    "sample_code": body.sample_code, "idempotent": True,
                    "message": "样本已登记（同内容重试幂等）"})
            raise ApiError(409, "SAMPLE_CONFLICT",
                           "样本编号已存在且登记内容不同（异内容冲突）")
        db.insert_sample(conn, body.sample_code, collected, body.collector, body.seal_number)
    return {"sample_code": body.sample_code, "idempotent": False,
            "collected_at": collected, "message": "登记成功"}


@app.get("/api/samples", tags=["samples"])
def get_samples() -> dict[str, Any]:
    rows = db.list_samples()
    return {"count": len(rows), "samples": [_summary(r) for r in rows]}


# ---------------- 事件接收 ----------------

@app.post("/api/events", tags=["events"], status_code=201)
def create_event(body: EventIn) -> dict[str, Any]:
    payload = _payload_for_event(body)
    occurred = payload["occurred_at"]

    with db.write_tx() as conn:
        sample = conn.execute(
            "SELECT * FROM samples WHERE sample_code=?", (body.sample_code,)
        ).fetchone()
        if sample is None:
            raise ApiError(404, "SAMPLE_NOT_FOUND",
                           f"样本不存在：{body.sample_code}，请先登记")
        dup = conn.execute(
            "SELECT * FROM custody_events WHERE event_no=?", (body.event_no,)
        ).fetchone()
        if dup is not None:
            dup_payload = json.loads(dup["payload_json"])
            # 同内容 -> 幂等（即使样本已核定，重放同一历史事件也允许）；异内容 -> 冲突
            if event_content_hash(dup_payload) == event_content_hash(payload):
                return JSONResponse(status_code=200, content={
                    "event_no": body.event_no, "idempotent": True,
                    "message": "事件已存在（同内容重试幂等）",
                    "verification": _verify_locked(conn, body.sample_code)})
            raise ApiError(409, "EVENT_CONFLICT",
                           "事件号已存在但内容不同（异内容冲突）",
                           {"existing_event_no": body.event_no})

        # 新事件：核定后拒绝改写历史
        _require_sample_unlocked(sample)

        db.insert_event(conn, body.event_no, body.sample_code, body.event_type,
                        occurred, payload)
        result = _verify_locked(conn, body.sample_code)

    return {"event_no": body.event_no, "idempotent": False,
            "message": "事件已接收，证据链已按发生时间重算",
            "verification": result}


def _verify_locked(conn, sample_code: str) -> dict[str, Any]:
    sample = conn.execute("SELECT * FROM samples WHERE sample_code=?",
                          (sample_code,)).fetchone()
    events = list(conn.execute(
        "SELECT * FROM custody_events WHERE sample_code=? ORDER BY occurred_at, id",
        (sample_code,)))
    return verify_sample(sample, events)


@app.get("/api/events/{event_no}", tags=["events"])
def get_event(event_no: str) -> dict[str, Any]:
    row = db.get_event(event_no)
    if row is None:
        raise ApiError(404, "EVENT_NOT_FOUND", f"事件不存在：{event_no}")
    return {"event_no": row["event_no"], "sample_code": row["sample_code"],
            "event_type": row["event_type"], "occurred_at": row["occurred_at"],
            "recorded_at": row["recorded_at"],
            "payload": json.loads(row["payload_json"])}


# ---------------- 证据链查询 ----------------

@app.get("/api/samples/{sample_code}", tags=["samples"])
def get_sample_chain(sample_code: str) -> dict[str, Any]:
    sample = db.get_sample(sample_code)
    if sample is None:
        raise ApiError(404, "SAMPLE_NOT_FOUND", f"样本不存在：{sample_code}")
    result = verify_sample(sample, db.list_events(sample_code))
    if sample["approved"] and sample["snapshot_json"]:
        result["approval_snapshot"] = json.loads(sample["snapshot_json"])
    return result


@app.get("/api/samples/{sample_code}/events", tags=["samples"])
def get_sample_events(sample_code: str) -> dict[str, Any]:
    sample = db.get_sample(sample_code)
    if sample is None:
        raise ApiError(404, "SAMPLE_NOT_FOUND", f"样本不存在：{sample_code}")
    rows = db.list_events(sample_code)
    return {
        "sample_code": sample_code,
        "count": len(rows),
        "events": [
            {"event_no": r["event_no"], "event_type": r["event_type"],
             "occurred_at": r["occurred_at"], "recorded_at": r["recorded_at"],
             "payload": json.loads(r["payload_json"])}
            for r in rows
        ],
    }


# ---------------- 核定入库 ----------------

@app.post("/api/samples/{sample_code}/approve", tags=["samples"])
def approve(sample_code: str, body: ApproveIn) -> dict[str, Any]:
    with db.write_tx() as conn:
        sample = conn.execute(
            "SELECT * FROM samples WHERE sample_code=?", (sample_code,)
        ).fetchone()
        if sample is None:
            raise ApiError(404, "SAMPLE_NOT_FOUND", f"样本不存在：{sample_code}")
        if sample["approved"]:
            # 核定本身幂等：同人重复核定直接返回快照
            return {"sample_code": sample_code, "idempotent": True,
                    "message": "样本已核定（幂等返回）",
                    "approved_at": sample["approved_at"],
                    "snapshot": json.loads(sample["snapshot_json"])}

        events = list(conn.execute(
            "SELECT * FROM custody_events WHERE sample_code=? ORDER BY occurred_at, id",
            (sample_code,)))
        result = verify_sample(sample, events)
        if result["status"] != "valid":
            raise ApiError(
                422, "NOT_APPROVABLE",
                "仅有效链可核定入库",
                {"status": result["status"],
                 "reasons": result["reasons"],
                 "pending_reasons": result["pending_reasons"]},
            )

        approved_at = now_ts()
        snapshot = {"approved_at": approved_at, "approver": body.approver,
                    "note": body.note, "result": result}
        db.approve_sample(conn, sample_code, body.approver, approved_at, body.note, result)
    return {"sample_code": sample_code, "idempotent": False,
            "message": "核定入库成功，历史记录已锁定",
            "approved_at": approved_at, "snapshot": snapshot}
