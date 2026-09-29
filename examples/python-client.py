#!/usr/bin/env python3
"""样本交接链核验服务 —— Python 调用示例（仅标准库，无第三方依赖）。

用法：
    python examples/python-client.py [base_url]
例如：
    python examples/python-client.py http://localhost:8080
"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8080"
SAMPLE = "S-PY-01"
SEAL = "SEAL-P1"


def call(method: str, path: str, body: dict | None = None, *,
         allow_status: tuple[int, ...] = ()) -> tuple[int, dict]:
    data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
    req = urllib.request.Request(
        BASE + path, data=data, method=method,
        headers={"Content-Type": "application/json; charset=utf-8"})
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        if e.code in allow_status:
            return e.code, json.loads(e.read().decode("utf-8"))
        raise


def show(title: str, status: int, body: dict) -> None:
    print(f"\n=== {title} [HTTP {status}] ===")
    print(json.dumps(body, ensure_ascii=False, indent=2)[:1200])


def main() -> None:
    code, body = call("GET", "/health")
    print("health:", body)

    # 1. 登记
    code, body = call("POST", "/api/samples", {
        "sample_code": SAMPLE, "collected_at": "2026-09-29T08:00:00Z",
        "collector": "张采集", "seal_number": SEAL})
    show("样本登记", code, body)

    # 2. 同内容重试 -> 200 幂等
    code, _ = call("POST", "/api/samples", {
        "sample_code": SAMPLE, "collected_at": "2026-09-29T08:00:00Z",
        "collector": "张采集", "seal_number": SEAL}, allow_status=(200,))
    print("同内容重试状态码:", code, "(200=幂等)")

    # 3. 移交
    call("POST", "/api/events", {
        "event_no": "P-T1", "sample_code": SAMPLE, "event_type": "transfer",
        "occurred_at": "2026-09-29T09:00:00Z",
        "from_custodian": "张采集", "to_custodian": "钱运输", "seal_number": SEAL})
    # 4. 签收
    call("POST", "/api/events", {
        "event_no": "P-R1", "sample_code": SAMPLE, "event_type": "receipt",
        "occurred_at": "2026-09-29T12:00:00Z", "transfer_no": "P-T1",
        "receiver": "钱运输", "seal_number": SEAL})
    # 5. 封签检查
    call("POST", "/api/events", {
        "event_no": "P-C1", "sample_code": SAMPLE, "event_type": "seal_check",
        "occurred_at": "2026-09-29T12:30:00Z", "checker": "陈验收",
        "seal_result": "intact", "seal_number": SEAL})
    # 6. 入库（这里故意乱序：前面流程完整，此条仅演示统一入口）
    code, body = call("POST", "/api/events", {
        "event_no": "P-W1", "sample_code": SAMPLE, "event_type": "storage",
        "occurred_at": "2026-09-29T15:00:00Z", "lab": "中心实验室一号库",
        "seal_number": SEAL})
    v = body["verification"]
    show("入库后即时重算结果", code, {
        "status": v["status"],
        "current_custodian": v["current_custodian"],
        "chain_order": [e["event_no"] for e in v["chain"]],
    })

    # 7. 异内容冲突 -> 409
    code, body = call("POST", "/api/events", {
        "event_no": "P-T1", "sample_code": SAMPLE, "event_type": "transfer",
        "occurred_at": "2026-09-29T09:00:00Z",
        "from_custodian": "张采集", "to_custodian": "其他人", "seal_number": SEAL},
        allow_status=(409,))
    print("异内容冲突状态码:", code, body["error"]["code"])

    # 8. 查询证据链
    code, body = call("GET", f"/api/samples/{SAMPLE}")
    show("证据链 / 当前保管人 / 结论", code, {
        "sample_code": body["sample_code"],
        "status": body["status"],
        "current_custodian": body["current_custodian"],
        "pending_reasons": body["pending_reasons"],
        "reasons": body["reasons"],
        "chain": [{"event_no": e["event_no"], "type": e["event_type"],
                   "at": e["occurred_at"], "valid": e["valid"],
                   "issues": e["issues"]} for e in body["chain"]],
    })

    # 9. 核定入库
    code, body = call("POST", f"/api/samples/{SAMPLE}/approve",
                      {"approver": "质量总监", "note": "链条完整"})
    show("核定入库", code, {"message": body["message"], "approved_at": body["approved_at"]})

    # 10. 核定后改写 -> 409
    code, body = call("POST", "/api/events", {
        "event_no": "P-X1", "sample_code": SAMPLE, "event_type": "seal_check",
        "occurred_at": "2026-09-29T16:00:00Z", "checker": "某人",
        "seal_number": SEAL}, allow_status=(409,))
    print("核定后改写状态码:", code, body["error"]["code"])


if __name__ == "__main__":
    main()
