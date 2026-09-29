#!/usr/bin/env bash
# 样本交接链核验服务 —— curl 全流程调用示例
# 用法：bash examples/curl-examples.sh [BASE_URL]
set -euo pipefail

BASE="${1:-http://localhost:8080}"
C="curl -fsS"

echo "健康检查: $BASE/health"
curl -fsS "$BASE/health"; echo

echo
echo "== 1. 样本登记 =="
curl -fsS -X POST "$BASE/api/samples" -H 'Content-Type: application/json' -d '{
  "sample_code": "S-CURL-01",
  "collected_at": "2026-09-29T08:00:00Z",
  "collector": "张采集",
  "seal_number": "SEAL-C1"
}'; echo

echo
echo "== 2. 同内容重试（200，幂等） =="
curl -s -o /dev/null -w "HTTP %{http_code}\n" -X POST "$BASE/api/samples" -H 'Content-Type: application/json' -d '{
  "sample_code": "S-CURL-01",
  "collected_at": "2026-09-29T08:00:00Z",
  "collector": "张采集",
  "seal_number": "SEAL-C1"
}'

echo
echo "== 3. 移交（必须由当前保管人发起） =="
curl -fsS -X POST "$BASE/api/events" -H 'Content-Type: application/json' -d '{
  "event_no": "C-T1", "sample_code": "S-CURL-01", "event_type": "transfer",
  "occurred_at": "2026-09-29T09:00:00Z",
  "from_custodian": "张采集", "to_custodian": "钱运输", "seal_number": "SEAL-C1"
}'; echo

echo
echo "== 4. 签收（匹配移交单号/接收人/封签号，且晚于移交） =="
curl -fsS -X POST "$BASE/api/events" -H 'Content-Type: application/json' -d '{
  "event_no": "C-R1", "sample_code": "S-CURL-01", "event_type": "receipt",
  "occurred_at": "2026-09-29T12:00:00Z", "transfer_no": "C-T1",
  "receiver": "钱运输", "seal_number": "SEAL-C1"
}'; echo

echo
echo "== 5. 封签检查 =="
curl -fsS -X POST "$BASE/api/events" -H 'Content-Type: application/json' -d '{
  "event_no": "C-C1", "sample_code": "S-CURL-01", "event_type": "seal_check",
  "occurred_at": "2026-09-29T12:30:00Z", "checker": "陈验收",
  "seal_result": "intact", "seal_number": "SEAL-C1"
}'; echo

echo
echo "== 6. 实验室入库 =="
curl -fsS -X POST "$BASE/api/events" -H 'Content-Type: application/json' -d '{
  "event_no": "C-W1", "sample_code": "S-CURL-01", "event_type": "storage",
  "occurred_at": "2026-09-29T15:00:00Z", "lab": "中心实验室一号库",
  "seal_number": "SEAL-C1"
}'; echo

echo
echo "== 7. 查询证据链（按发生时间排列）、当前保管人、结论 =="
curl -fsS "$BASE/api/samples/S-CURL-01" | python3 -m json.tool --no-ensure-ascii

echo
echo "== 8. 事件号同内容重试（200，幂等）/ 异内容（409，冲突） =="
curl -s -o /dev/null -w "same-content   -> HTTP %{http_code}\n" -X POST "$BASE/api/events" -H 'Content-Type: application/json' -d '{
  "event_no": "C-T1", "sample_code": "S-CURL-01", "event_type": "transfer",
  "occurred_at": "2026-09-29T09:00:00Z",
  "from_custodian": "张采集", "to_custodian": "钱运输", "seal_number": "SEAL-C1"
}'
curl -s -o /dev/null -w "diff-content   -> HTTP %{http_code}\n" -X POST "$BASE/api/events" -H 'Content-Type: application/json' -d '{
  "event_no": "C-T1", "sample_code": "S-CURL-01", "event_type": "transfer",
  "occurred_at": "2026-09-29T09:00:00Z",
  "from_custodian": "张采集", "to_custodian": "冒名者", "seal_number": "SEAL-C1"
}'

echo
echo "== 9. 核定入库（仅 valid），核定后历史锁定 =="
curl -fsS -X POST "$BASE/api/samples/S-CURL-01/approve" \
  -H 'Content-Type: application/json' \
  -d '{"approver":"质量总监","note":"链条完整，准予入库"}' | python3 -m json.tool --no-ensure-ascii

echo
echo "== 10. 核定后新增事件（409 HISTORY_LOCKED） =="
curl -s -w "\nHTTP %{http_code}\n" -X POST "$BASE/api/events" -H 'Content-Type: application/json' -d '{
  "event_no": "C-X1", "sample_code": "S-CURL-01", "event_type": "seal_check",
  "occurred_at": "2026-09-29T16:00:00Z", "checker": "某人", "seal_number": "SEAL-C1"
}'

echo
echo "演示：查询内置无效样本（重复签收，无效优先）"
curl -fsS "$BASE/api/samples/S-INVALID-003" \
  | python3 -c "import json,sys;d=json.load(sys.stdin);print('status =',d['status']);[print(' -',r['code'],r['message']) for r in d['reasons']]"
