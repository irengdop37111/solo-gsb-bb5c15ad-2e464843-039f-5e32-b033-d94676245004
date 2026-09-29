"""核验规则测试：有效链、待补证、无效及无效优先。"""
from __future__ import annotations

from .conftest import event, register

SEAL = "SEAL-1"


def _full_valid_chain(client, code="S-V"):
    register(client, code=code, seal=SEAL, collector="采集人甲")
    assert event(client, event_no="T1", sample_code=code, event_type="transfer",
                 occurred_at="2026-09-20T09:00:00Z", from_custodian="采集人甲",
                 to_custodian="运输员乙", seal_number=SEAL).status_code == 201
    assert event(client, event_no="R1", sample_code=code, event_type="receipt",
                 occurred_at="2026-09-20T12:00:00Z", transfer_no="T1",
                 receiver="运输员乙", seal_number=SEAL).status_code == 201
    assert event(client, event_no="C1", sample_code=code, event_type="seal_check",
                 occurred_at="2026-09-20T12:30:00Z", checker="验收员丙",
                 seal_result="intact", seal_number=SEAL).status_code == 201
    assert event(client, event_no="W1", sample_code=code, event_type="storage",
                 occurred_at="2026-09-20T15:00:00Z", lab="一号实验室",
                 seal_number=SEAL).status_code == 201


def test_full_valid_chain(client):
    _full_valid_chain(client)
    r = client.get("/api/samples/S-V")
    assert r.status_code == 200
    data = r.json()
    assert data["status"] == "valid"
    assert data["current_custodian"] == "运输员乙"
    assert data["in_transit"] is False
    assert [e["event_type"] for e in data["chain"]] == [
        "transfer", "receipt", "seal_check", "storage"]
    assert all(e["valid"] for e in data["chain"])
    assert data["reasons"] == []


def test_pending_transfer_open(client):
    register(client, code="S-P1", seal=SEAL)
    event(client, event_no="T1", sample_code="S-P1", event_type="transfer",
          occurred_at="2026-09-20T09:00:00Z", from_custodian="采集人甲",
          to_custodian="运输员乙", seal_number=SEAL)
    data = client.get("/api/samples/S-P1").json()
    assert data["status"] == "pending"
    assert data["in_transit"] is True
    codes = {p["code"] for p in data["pending_reasons"]}
    assert {"TRANSFER_OPEN", "NO_STORAGE"} <= codes


def test_pending_signed_no_storage(client):
    _full_valid_chain(client, code="S-P2")
    # 去掉入库：重新注册一个只到签收的样本
    register(client, code="S-P3", seal=SEAL, collector="采集人甲")
    event(client, event_no="T9", sample_code="S-P3", event_type="transfer",
          occurred_at="2026-09-20T09:00:00Z", from_custodian="采集人甲",
          to_custodian="运输员乙", seal_number=SEAL)
    event(client, event_no="R9", sample_code="S-P3", event_type="receipt",
          occurred_at="2026-09-20T12:00:00Z", transfer_no="T9",
          receiver="运输员乙", seal_number=SEAL)
    data = client.get("/api/samples/S-P3").json()
    assert data["status"] == "pending"
    assert {p["code"] for p in data["pending_reasons"]} == {"NO_STORAGE"}


def test_transfer_must_be_initiated_by_current_custodian(client):
    register(client, code="S-I1", seal=SEAL, collector="采集人甲")
    event(client, event_no="T1", sample_code="S-I1", event_type="transfer",
          occurred_at="2026-09-20T09:00:00Z", from_custodian="陌生人X",
          to_custodian="运输员乙", seal_number=SEAL)
    event(client, event_no="R1", sample_code="S-I1", event_type="receipt",
          occurred_at="2026-09-20T12:00:00Z", transfer_no="T1",
          receiver="运输员乙", seal_number=SEAL)
    event(client, event_no="W1", sample_code="S-I1", event_type="storage",
          occurred_at="2026-09-20T15:00:00Z", lab="一号实验室", seal_number=SEAL)
    data = client.get("/api/samples/S-I1").json()
    # 无效优先：即使签收/入库都在，仍为无效
    assert data["status"] == "invalid"
    assert data["reasons"][0]["code"] == "CHAIN_BROKEN_TRANSFER_CUSTODIAN"
    # 保管人未被非法转移
    assert data["current_custodian"] == "采集人甲"


def test_no_second_transfer_before_receipt(client):
    register(client, code="S-I2", seal=SEAL, collector="采集人甲")
    event(client, event_no="T1", sample_code="S-I2", event_type="transfer",
          occurred_at="2026-09-20T09:00:00Z", from_custodian="采集人甲",
          to_custodian="运输员乙", seal_number=SEAL)
    event(client, event_no="T2", sample_code="S-I2", event_type="transfer",
          occurred_at="2026-09-20T10:00:00Z", from_custodian="采集人甲",
          to_custodian="运输员丁", seal_number=SEAL)
    data = client.get("/api/samples/S-I2").json()
    assert data["status"] == "invalid"
    assert any(r["code"] == "CHAIN_BROKEN_DOUBLE_TRANSFER" for r in data["reasons"])


def test_receipt_must_match_transfer_receiver_seal_and_time(client):
    register(client, code="S-I3", seal=SEAL, collector="采集人甲")
    event(client, event_no="T1", sample_code="S-I3", event_type="transfer",
          occurred_at="2026-09-20T09:00:00Z", from_custodian="采集人甲",
          to_custodian="运输员乙", seal_number=SEAL)

    # 接收人不匹配
    r = event(client, event_no="R1", sample_code="S-I3", event_type="receipt",
              occurred_at="2026-09-20T12:00:00Z", transfer_no="T1",
              receiver="冒名者", seal_number=SEAL)
    assert r.status_code == 201
    # 封签号不匹配（另一次尝试：对同一未闭合移交单）
    r2 = event(client, event_no="R2", sample_code="S-I3", event_type="receipt",
               occurred_at="2026-09-20T13:00:00Z", transfer_no="T1",
               receiver="运输员乙", seal_number="SEAL-OTHER")
    assert r2.status_code == 201
    data = client.get("/api/samples/S-I3").json()
    codes = {x["code"] for x in data["reasons"]}
    assert "RECEIPT_WRONG_RECEIVER" in codes
    assert "SEAL_MISMATCH" in codes
    assert data["status"] == "invalid"
    # 两次签收都无效，保管人仍是采集人
    assert data["current_custodian"] == "采集人甲"


def test_receipt_before_transfer_is_invalid(client):
    register(client, code="S-I4", seal=SEAL, collector="采集人甲")
    # 签收发生在移交“之前”（按发生时间），但先录入移交事件号
    event(client, event_no="T1", sample_code="S-I4", event_type="transfer",
          occurred_at="2026-09-20T12:00:00Z", from_custodian="采集人甲",
          to_custodian="运输员乙", seal_number=SEAL)
    event(client, event_no="R1", sample_code="S-I4", event_type="receipt",
          occurred_at="2026-09-20T09:00:00Z", transfer_no="T1",
          receiver="运输员乙", seal_number=SEAL)
    data = client.get("/api/samples/S-I4").json()
    assert data["status"] == "invalid"
    assert any(r["code"] == "RECEIPT_BEFORE_TRANSFER" for r in data["reasons"])


def test_duplicate_receipt_invalid_even_with_storage(client):
    register(client, code="S-I5", seal=SEAL, collector="采集人甲")
    event(client, event_no="T1", sample_code="S-I5", event_type="transfer",
          occurred_at="2026-09-20T09:00:00Z", from_custodian="采集人甲",
          to_custodian="运输员乙", seal_number=SEAL)
    event(client, event_no="R1", sample_code="S-I5", event_type="receipt",
          occurred_at="2026-09-20T12:00:00Z", transfer_no="T1",
          receiver="运输员乙", seal_number=SEAL)
    event(client, event_no="R2", sample_code="S-I5", event_type="receipt",
          occurred_at="2026-09-20T13:00:00Z", transfer_no="T1",
          receiver="运输员乙", seal_number=SEAL)
    event(client, event_no="W1", sample_code="S-I5", event_type="storage",
          occurred_at="2026-09-20T15:00:00Z", lab="一号实验室", seal_number=SEAL)
    data = client.get("/api/samples/S-I5").json()
    assert data["status"] == "invalid"
    assert any(r["code"] == "DUPLICATE_RECEIPT" for r in data["reasons"])


def test_seal_mismatch_on_storage_invalid_priority(client):
    register(client, code="S-I6", seal=SEAL, collector="采集人甲")
    event(client, event_no="T1", sample_code="S-I6", event_type="transfer",
          occurred_at="2026-09-20T09:00:00Z", from_custodian="采集人甲",
          to_custodian="运输员乙", seal_number=SEAL)
    event(client, event_no="R1", sample_code="S-I6", event_type="receipt",
          occurred_at="2026-09-20T12:00:00Z", transfer_no="T1",
          receiver="运输员乙", seal_number=SEAL)
    event(client, event_no="W1", sample_code="S-I6", event_type="storage",
          occurred_at="2026-09-20T15:00:00Z", lab="一号实验室",
          seal_number="SEAL-BROKEN")
    data = client.get("/api/samples/S-I6").json()
    assert data["status"] == "invalid"
    assert any(r["code"] == "SEAL_MISMATCH" for r in data["reasons"])
