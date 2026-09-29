"""交接链核验核心：按发生时间重放事件，输出证据链、当前保管人与结论。

结论：
- valid   有效：保管链连续，且存在有效签收与有效入库；
- pending 待补证：链未判无效，但缺签收或缺入库（含移交后尚无签收）；
- invalid 无效：保管人断链 / 重复签收 / 封签不符等（无效优先于待补证）。

规则要点：
- 移交须由当前保管人发起；移交与下一次签收之间不得再次移交；
- 签收须匹配未闭合移交单号、接收人、封签号，且发生时间严格晚于移交；
- 仅有效链（valid）允许核定入库，核定后历史锁定。

重放完全基于事件的 occurred_at（而不是 recorded_at 录入顺序），
因此乱序补录后重新调用核验即可得到重算结果。

实现上重放两遍：
1) 预演确定每个移交事件在保管状态机中是否“结构合法”；
2) 正式重放输出证据链。签收按移交单号可匹配到时间更晚才出现的合法移交，
   从而把“签收发生在移交之前”与“找不到移交单”区分开。
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from typing import Any, Optional


# ---------------- 时间与内容指纹 ----------------

def parse_ts(value: str) -> datetime:
    """解析 ISO-8601（兼容尾部 Z）为带时区的 UTC datetime。"""
    v = value.strip()
    if v.endswith("Z"):
        v = v[:-1] + "+00:00"
    dt = datetime.fromisoformat(v)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def canonical_ts(value: str) -> str:
    dt = parse_ts(value)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond:06d}Z"


def now_ts() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def event_content_hash(payload: dict[str, Any]) -> str:
    """事件业务内容指纹（不含 event_no 本身），用于同号重试的内容比对。"""
    import hashlib
    body = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


# ---------------- 无效/待补证原因 ----------------

REASONS = {
    "SEAL_MISMATCH": "封签不符：事件封签号与样本登记封签号不一致",
    "CHAIN_BROKEN_TRANSFER_CUSTODIAN": "保管人断链：移交人不是当前保管人",
    "CHAIN_BROKEN_DOUBLE_TRANSFER": "保管人断链：移交尚未签收，期间不得再次移交",
    "DUPLICATE_RECEIPT": "重复签收：该移交单已被签收",
    "RECEIPT_NO_MATCH_TRANSFER": "保管人断链：签收找不到与之匹配的有效移交单",
    "RECEIPT_WRONG_RECEIVER": "保管人断链：签收人与移交单接收人不一致",
    "RECEIPT_BEFORE_TRANSFER": "保管人断链：签收时间早于或等于移交时间",
    "EVENT_BEFORE_COLLECTION": "保管人断链：事件发生时间早于样本采集时间",
}

PENDING_REASONS = {
    "NO_RECEIPT": "缺签收：尚无任何有效签收记录",
    "TRANSFER_OPEN": "待补证：存在移交后尚未签收的在途环节",
    "NO_STORAGE": "缺入库：尚无实验室入库记录",
}


def _row_payload(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
    if isinstance(row, dict) and "payload_json" in row:
        return json.loads(row["payload_json"])
    if isinstance(row, sqlite3.Row):
        return json.loads(row["payload_json"])
    return row  # already payload dict (unit tests)


def _all_transfer_index(events: list, sample_seal: str) -> dict[str, dict[str, Any]]:
    """事件号 -> 移交静态信息（无论是否结构合法、无论时间先后）。"""
    index: dict[str, dict[str, Any]] = {}
    for row in events:
        if row["event_type"] != "transfer":
            continue
        p = _row_payload(row)
        index[row["event_no"]] = {
            "event_no": row["event_no"],
            "to": p.get("to_custodian"),
            "seal": p.get("seal_number"),
            "seal_ok": p.get("seal_number") == sample_seal,
            "occurred_at": row["occurred_at"],
        }
    return index


# ---------------- 重放核验 ----------------

def verify_chain(sample: sqlite3.Row | dict[str, Any], events: list) -> dict[str, Any]:
    """对单个样本的事件集合做确定性重放并给出结论。"""
    sample = dict(sample)
    sample_code = sample["sample_code"]
    sample_seal = sample["seal_number"]
    collected_at = parse_ts(sample["collected_at"])
    all_transfers = _all_transfer_index(events, sample_seal)

    # ---- 第一遍：预演，确定结构合法的移交集合 ----
    valid_transfers: set[str] = set()
    cur1: Optional[str] = sample["collector"]
    open1: Optional[str] = None
    for row in events:
        p = _row_payload(row)
        etype = row["event_type"]
        seal_ok = p.get("seal_number") == sample_seal
        if etype == "transfer":
            if (seal_ok
                    and p.get("from_custodian") == cur1
                    and open1 is None
                    and parse_ts(row["occurred_at"]) >= collected_at):
                valid_transfers.add(row["event_no"])
                open1 = row["event_no"]
        elif etype == "receipt" and seal_ok:
            t = all_transfers.get(p.get("transfer_no") or "")
            if (t and open1 == t["event_no"] and t["event_no"] in valid_transfers
                    and parse_ts(row["occurred_at"]) > parse_ts(t["occurred_at"])
                    and p.get("receiver") == t["to"]):
                cur1 = t["to"]
                open1 = None

    # ---- 第二遍：正式重放，输出证据链与原因 ----
    chain_invalid = False
    invalid_reasons: list[dict[str, str]] = []

    def mark_invalid(code: str, event_no: Optional[str], occurred_at: Optional[str],
                     detail: str = "") -> None:
        nonlocal chain_invalid
        chain_invalid = True
        invalid_reasons.append({
            "code": code,
            "event_no": event_no,
            "occurred_at": occurred_at,
            "message": REASONS[code] + (f"（{detail}）" if detail else ""),
        })

    current_custodian: Optional[str] = sample["collector"]
    open_transfer: Optional[dict[str, Any]] = None
    closed_transfers: set[str] = set()

    has_receipt = False
    has_storage = False
    chain_out: list[dict[str, Any]] = []

    for row in events:
        p = _row_payload(row)
        entry = {
            "event_no": row["event_no"],
            "event_type": row["event_type"],
            "occurred_at": row["occurred_at"],
            "recorded_at": row["recorded_at"],
            "seal_number": p.get("seal_number"),
            "valid": True,
            "issues": [],
            "detail": {},
        }
        ev_time = parse_ts(row["occurred_at"])
        seal_ok = p.get("seal_number") == sample_seal
        if not seal_ok:
            entry["valid"] = False
            entry["issues"].append("SEAL_MISMATCH")
            mark_invalid("SEAL_MISMATCH", row["event_no"], row["occurred_at"],
                         f"登记={sample_seal}，上报={p.get('seal_number')}")
        before_collection = ev_time < collected_at
        if before_collection:
            entry["valid"] = False
            entry["issues"].append("EVENT_BEFORE_COLLECTION")
            mark_invalid("EVENT_BEFORE_COLLECTION", row["event_no"], row["occurred_at"])

        etype = row["event_type"]

        if etype == "transfer":
            entry["detail"] = {
                "from_custodian": p.get("from_custodian"),
                "to_custodian": p.get("to_custodian"),
            }
            problems = []
            if p.get("from_custodian") != current_custodian:
                problems.append("CHAIN_BROKEN_TRANSFER_CUSTODIAN")
            if open_transfer is not None:
                problems.append("CHAIN_BROKEN_DOUBLE_TRANSFER")
            if problems:
                entry["valid"] = False
                entry["issues"].extend(problems)
                for code in problems:
                    detail = ""
                    if code == "CHAIN_BROKEN_TRANSFER_CUSTODIAN":
                        detail = f"移交人={p.get('from_custodian')}，当前保管人={current_custodian}"
                    else:
                        detail = f"未闭合移交单={open_transfer['event_no']}"
                    mark_invalid(code, row["event_no"], row["occurred_at"], detail)
            # 仅当封签相符且时间合法且结构合法时，才成为在途移交
            if seal_ok and not before_collection and not problems:
                open_transfer = {
                    "event_no": row["event_no"],
                    "to": p.get("to_custodian"),
                    "seal": p.get("seal_number"),
                    "occurred_at": row["occurred_at"],
                }
                entry["detail"]["state"] = "awaiting_receipt"

        elif etype == "receipt":
            entry["detail"] = {"receiver": p.get("receiver"),
                               "transfer_no": p.get("transfer_no")}
            tno = p.get("transfer_no") or ""
            target = all_transfers.get(tno)
            problems = []

            if target is None or tno not in valid_transfers:
                problems.append("RECEIPT_NO_MATCH_TRANSFER")
            elif tno in closed_transfers:
                problems.append("DUPLICATE_RECEIPT")
            else:
                if ev_time <= parse_ts(target["occurred_at"]):
                    problems.append("RECEIPT_BEFORE_TRANSFER")
                if p.get("receiver") != target["to"]:
                    problems.append("RECEIPT_WRONG_RECEIVER")

            if problems or not seal_ok or before_collection:
                entry["valid"] = False
                for code in problems:
                    entry["issues"].append(code)
                    detail = ""
                    if code == "RECEIPT_WRONG_RECEIVER":
                        detail = f"签收人={p.get('receiver')}，应为={target['to'] if target else None}"
                    elif code == "RECEIPT_NO_MATCH_TRANSFER":
                        detail = f"移交单号={tno}"
                    mark_invalid(code, row["event_no"], row["occurred_at"], detail)
            else:
                # 有效签收：保管权转移，移交单闭合
                current_custodian = target["to"]
                closed_transfers.add(tno)
                if open_transfer and open_transfer["event_no"] == tno:
                    open_transfer = None
                has_receipt = True
                entry["detail"]["matched_transfer_no"] = tno
                entry["detail"]["state"] = "received"

        elif etype == "seal_check":
            entry["detail"] = {
                "checker": p.get("checker"),
                "seal_result": p.get("seal_result", "intact"),
                "state": "checked",
            }

        elif etype == "storage":
            entry["detail"] = {"lab": p.get("lab"), "custodian": current_custodian,
                               "state": "stored"}
            has_storage = True

        chain_out.append(entry)

    # ---------------- 汇总结论（无效优先） ----------------
    pending: list[str] = []
    if chain_invalid:
        status = "invalid"
    else:
        if not has_receipt:
            pending.append("NO_RECEIPT")
        if open_transfer is not None:
            pending.append("TRANSFER_OPEN")
        if not has_storage:
            pending.append("NO_STORAGE")
        status = "valid" if not pending else "pending"

    return {
        "sample_code": sample_code,
        "status": status,
        "current_custodian": current_custodian,
        "in_transit": open_transfer is not None,
        "open_transfer_no": open_transfer["event_no"] if open_transfer else None,
        "has_valid_receipt": has_receipt,
        "has_storage": has_storage,
        "collected_at": sample["collected_at"],
        "collector": sample["collector"],
        "seal_number": sample_seal,
        "approved": bool(sample.get("approved")),
        "approved_at": sample.get("approved_at"),
        "approver": sample.get("approver"),
        "chain": chain_out,
        "reasons": invalid_reasons,
        "pending_reasons": [
            {"code": code, "message": PENDING_REASONS[code]} for code in pending
        ],
        "event_count": len(events),
    }


def verify_sample(sample: sqlite3.Row | dict[str, Any], events: list) -> dict[str, Any]:
    return verify_chain(sample, events)
