"""乱序补录、幂等/冲突、核定锁定相关 API 测试。"""
from __future__ import annotations

from .conftest import event, register

SEAL = "SEAL-1"


def test_out_of_order_recording_recomputed_by_occurred_at(client):
    register(client, code="S-O", seal=SEAL, collector="采集人甲")
    # 录入顺序：入库 -> 移交 -> 签收（发生时间顺序正常）
    event(client, event_no="W1", sample_code="S-O", event_type="storage",
          occurred_at="2026-09-20T15:00:00Z", lab="一号实验室", seal_number=SEAL)
    event(client, event_no="R1", sample_code="S-O", event_type="receipt",
          occurred_at="2026-09-20T12:00:00Z", transfer_no="T1",
          receiver="运输员乙", seal_number=SEAL)
    event(client, event_no="T1", sample_code="S-O", event_type="transfer",
          occurred_at="2026-09-20T09:00:00Z", from_custodian="采集人甲",
          to_custodian="运输员乙", seal_number=SEAL)

    data = client.get("/api/samples/S-O").json()
    assert [e["event_no"] for e in data["chain"]] == ["T1", "R1", "W1"]
    assert data["status"] == "valid"
    assert data["current_custodian"] == "运输员乙"


def test_late_supplement_changes_pending_to_valid(client):
    register(client, code="S-L", seal=SEAL, collector="采集人甲")
    event(client, event_no="T1", sample_code="S-L", event_type="transfer",
          occurred_at="2026-09-20T09:00:00Z", from_custodian="采集人甲",
          to_custodian="运输员乙", seal_number=SEAL)
    assert client.get("/api/samples/S-L").json()["status"] == "pending"

    # 补录签收与入库
    event(client, event_no="R1", sample_code="S-L", event_type="receipt",
          occurred_at="2026-09-20T12:00:00Z", transfer_no="T1",
          receiver="运输员乙", seal_number=SEAL)
    event(client, event_no="W1", sample_code="S-L", event_type="storage",
          occurred_at="2026-09-20T15:00:00Z", lab="一号实验室", seal_number=SEAL)
    assert client.get("/api/samples/S-L").json()["status"] == "valid"


def test_event_idempotent_same_content(client):
    register(client, code="S-E", seal=SEAL)
    payload = dict(event_no="T1", sample_code="S-E", event_type="transfer",
                   occurred_at="2026-09-20T09:00:00Z", from_custodian="采集人甲",
                   to_custodian="运输员乙", seal_number=SEAL)
    r1 = event(client, **payload)
    r2 = event(client, **payload)
    assert r1.status_code == 201 and r1.json()["idempotent"] is False
    assert r2.status_code == 200 and r2.json()["idempotent"] is True
    assert client.get("/api/samples/S-E/events").json()["count"] == 1


def test_event_conflict_different_content(client):
    register(client, code="S-F", seal=SEAL)
    event(client, event_no="T1", sample_code="S-F", event_type="transfer",
          occurred_at="2026-09-20T09:00:00Z", from_custodian="采集人甲",
          to_custodian="运输员乙", seal_number=SEAL)
    # 同事件号，不同接收人
    r = event(client, event_no="T1", sample_code="S-F", event_type="transfer",
              occurred_at="2026-09-20T09:00:00Z", from_custodian="采集人甲",
              to_custodian="其他人", seal_number=SEAL)
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "EVENT_CONFLICT"


def test_sample_registration_idempotent_and_conflict(client):
    r1 = client.post("/api/samples", json={
        "sample_code": "S-G", "collected_at": "2026-09-20T08:00:00Z",
        "collector": "采集人甲", "seal_number": SEAL})
    r2 = client.post("/api/samples", json={
        "sample_code": "S-G", "collected_at": "2026-09-20T08:00:00Z",
        "collector": "采集人甲", "seal_number": SEAL})
    assert r1.status_code == 201
    assert r2.status_code == 200 and r2.json()["idempotent"] is True

    r3 = client.post("/api/samples", json={
        "sample_code": "S-G", "collected_at": "2026-09-20T08:00:00Z",
        "collector": "采集人甲", "seal_number": "OTHER"})
    assert r3.status_code == 409
    assert r3.json()["error"]["code"] == "SAMPLE_CONFLICT"


def _make_valid(client, code="S-A"):
    register(client, code=code, seal=SEAL, collector="采集人甲")
    event(client, event_no="T1", sample_code=code, event_type="transfer",
          occurred_at="2026-09-20T09:00:00Z", from_custodian="采集人甲",
          to_custodian="运输员乙", seal_number=SEAL)
    event(client, event_no="R1", sample_code=code, event_type="receipt",
          occurred_at="2026-09-20T12:00:00Z", transfer_no="T1",
          receiver="运输员乙", seal_number=SEAL)
    event(client, event_no="W1", sample_code=code, event_type="storage",
          occurred_at="2026-09-20T15:00:00Z", lab="一号实验室", seal_number=SEAL)


def test_approve_only_valid_then_locks_history(client):
    _make_valid(client, "S-A")
    r = client.post("/api/samples/S-A/approve", json={"approver": "质量负责人"})
    assert r.status_code == 200
    assert r.json()["message"].startswith("核定入库成功")

    # 重复核定幂等
    r2 = client.post("/api/samples/S-A/approve", json={"approver": "质量负责人"})
    assert r2.json()["idempotent"] is True

    # 核定后改写历史被拒绝：新增事件
    r3 = event(client, event_no="X1", sample_code="S-A", event_type="seal_check",
               occurred_at="2026-09-20T16:00:00Z", checker="某人",
               seal_number=SEAL)
    assert r3.status_code == 409
    assert r3.json()["error"]["code"] == "HISTORY_LOCKED"

    # 同内容重试仍允许（幂等）
    r4 = event(client, event_no="T1", sample_code="S-A", event_type="transfer",
               occurred_at="2026-09-20T09:00:00Z", from_custodian="采集人甲",
               to_custodian="运输员乙", seal_number=SEAL)
    assert r4.status_code == 200 and r4.json()["idempotent"] is True


def test_approve_rejected_for_pending_and_invalid(client):
    register(client, code="S-P", seal=SEAL)
    r = client.post("/api/samples/S-P/approve", json={"approver": "质量负责人"})
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "NOT_APPROVABLE"

    register(client, code="S-I", seal=SEAL, collector="采集人甲")
    event(client, event_no="T1", sample_code="S-I", event_type="transfer",
          occurred_at="2026-09-20T09:00:00Z", from_custodian="陌生人",
          to_custodian="运输员乙", seal_number=SEAL)
    r = client.post("/api/samples/S-I/approve", json={"approver": "质量负责人"})
    assert r.status_code == 422


def test_missing_type_specific_field_rejected(client):
    register(client, code="S-M", seal=SEAL)
    # receipt 缺 transfer_no / receiver
    r = event(client, event_no="R1", sample_code="S-M", event_type="receipt",
              occurred_at="2026-09-20T12:00:00Z", seal_number=SEAL)
    assert r.status_code == 422
    assert set(r.json()["error"]["details"]) == {"transfer_no", "receiver"}


def test_event_for_unknown_sample_404(client):
    r = event(client, event_no="T1", sample_code="NOPE", event_type="seal_check",
              occurred_at="2026-09-20T12:00:00Z", checker="某人", seal_number="X")
    assert r.status_code == 404
