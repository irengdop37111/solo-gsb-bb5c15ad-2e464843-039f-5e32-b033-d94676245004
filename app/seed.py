"""首次启动演示数据：空库时写入 5 个覆盖各种结论的样本。

注意：事件的插入顺序故意与发生时间不一致，以演示“乱序补录按发生时间重算”。
"""
from __future__ import annotations

import json

from . import database as db

# (sample_code, collected_at, collector, seal_number)
SAMPLES = [
    ("S-VALID-001", "2026-09-20T08:00:00Z", "张采集", "SEAL-1001"),
    ("S-PEND-002", "2026-09-21T09:00:00Z", "李采集", "SEAL-1002"),
    ("S-INVALID-003", "2026-09-22T10:00:00Z", "王采集", "SEAL-1003"),
    ("S-INVALID-004", "2026-09-23T07:30:00Z", "赵采集", "SEAL-1004"),
    ("S-PEND-005", "2026-09-24T11:00:00Z", "孙采集", "SEAL-1005"),
]


def ev(event_no: str, sample_code: str, event_type: str, occurred_at: str, **payload) -> tuple:
    payload.setdefault("sample_code", sample_code)
    return (event_no, sample_code, event_type, occurred_at,
            json.dumps(payload, ensure_ascii=False, sort_keys=True))


# 插入顺序故意打乱（recorded_at 与 occurred_at 不一致）
EVENTS = [
    # S-VALID-001：完整有效链（入库，可核定）
    ev("E-1001-04", "S-VALID-001", "storage", "2026-09-20T15:00:00Z",
      lab="中心实验室一号库", seal_number="SEAL-1001"),
    ev("E-1001-01", "S-VALID-001", "transfer", "2026-09-20T09:00:00Z",
      from_custodian="张采集", to_custodian="周运输", seal_number="SEAL-1001"),
    ev("E-1001-03", "S-VALID-001", "seal_check", "2026-09-20T12:30:00Z",
      checker="陈验收", seal_result="intact", seal_number="SEAL-1001"),
    ev("E-1001-02", "S-VALID-001", "receipt", "2026-09-20T12:00:00Z",
      transfer_no="E-1001-01", receiver="周运输", seal_number="SEAL-1001"),

    # S-PEND-002：移交后未签收，也无入库 -> 待补证（TRANSFER_OPEN / NO_STORAGE）
    ev("E-1002-01", "S-PEND-002", "transfer", "2026-09-21T10:00:00Z",
      from_custodian="李采集", to_custodian="吴运输", seal_number="SEAL-1002"),

    # S-INVALID-003：同一移交单被签收两次 -> 无效优先（即使已有入库）
    ev("E-1003-01", "S-INVALID-003", "transfer", "2026-09-22T11:00:00Z",
      from_custodian="王采集", to_custodian="郑运输", seal_number="SEAL-1003"),
    ev("E-1003-02", "S-INVALID-003", "receipt", "2026-09-22T13:00:00Z",
      transfer_no="E-1003-01", receiver="郑运输", seal_number="SEAL-1003"),
    ev("E-1003-03", "S-INVALID-003", "receipt", "2026-09-22T13:30:00Z",
      transfer_no="E-1003-01", receiver="郑运输", seal_number="SEAL-1003"),
    ev("E-1003-04", "S-INVALID-003", "storage", "2026-09-22T15:00:00Z",
      lab="中心实验室二号库", seal_number="SEAL-1003"),

    # S-INVALID-004：入库时封签不符 -> 无效
    ev("E-1004-01", "S-INVALID-004", "transfer", "2026-09-23T08:00:00Z",
      from_custodian="赵采集", to_custodian="冯运输", seal_number="SEAL-1004"),
    ev("E-1004-02", "S-INVALID-004", "receipt", "2026-09-23T10:00:00Z",
      transfer_no="E-1004-01", receiver="冯运输", seal_number="SEAL-1004"),
    ev("E-1004-03", "S-INVALID-004", "storage", "2026-09-23T11:00:00Z",
      lab="中心实验室一号库", seal_number="SEAL-9999"),

    # S-PEND-005：已签收但未入库 -> 待补证（NO_STORAGE）
    ev("E-1005-02", "S-PEND-005", "receipt", "2026-09-24T14:00:00Z",
      transfer_no="E-1005-01", receiver="蒋运输", seal_number="SEAL-1005"),
    ev("E-1005-01", "S-PEND-005", "transfer", "2026-09-24T12:00:00Z",
      from_custodian="孙采集", to_custodian="蒋运输", seal_number="SEAL-1005"),
]


def seed_if_empty() -> bool:
    """空库时填充演示数据，返回是否执行了填充。"""
    with db.write_tx() as conn:
        if db.count_samples(conn) > 0:
            return False
        db.bulk_insert_demo(conn, SAMPLES, EVENTS)
        return True
