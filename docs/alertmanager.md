# Tích hợp Alertmanager

## Cấu hình tối thiểu

```yaml
receivers:
  - name: opstrail
    webhook_configs:
      - url: http://opstrail:8000/api/v1/webhooks/alertmanager
        send_resolved: true          # BẮT BUỘC
        http_config:
          authorization:
            type: Bearer
            credentials: <INTAKE_TOKEN>
```

> Thiếu nó thì alert **không bao giờ** rời khỏi trang [Firing](ui.md#đang-cháy-chưa-ai-nhận) — nó sẽ phình ra cho tới khi trang đó vô dụng.

## `send_resolved: true` là bắt buộc

Không có nó, OpsTrail **không bao giờ** biết alert kết thúc lúc nào:

- `ends_at` luôn NULL → mọi alert trông như đang firing vĩnh viễn
- Biểu đồ "how long alerts stay firing" rỗng
- Tile "still firing" trên dashboard chỉ tăng, không bao giờ giảm

## Xác thực

`INTAKE_TOKEN` là bearer token riêng cho máy, **không** dùng session của người.
Alertmanager là client máy; trộn hai thứ nghĩa là token bị lộ sẽ mở luôn quyền
quản trị.

Để trống `INTAKE_TOKEN` là **tắt kiểm tra** — chỉ chấp nhận được trên mạng nội
bộ đã tin cậy.

## Nên gửi kèm label gì

OpsTrail đọc nhãn theo thứ tự này (xem [Gán nhãn](labelling.md)):

| Chiều | Key, theo thứ tự ưu tiên |
|---|---|
| service | `service` → `service_name` → `servicename` → `app` → `application` → `component` → `job` |
| team | `team` → `squad` → `owner` → `owner_team` |

Rẻ nhất và chính xác nhất là thêm thẳng vào alerting rule:

```yaml
groups:
  - name: payments
    rules:
      - alert: PostgresqlDown
        expr: up{job="postgres"} == 0
        for: 2m
        labels:
          severity: critical
          service: payments-api     # khớp services.key trong OpsTrail
          team: payments            # khớp teams.key
        annotations:
          summary: "Postgres down ({{ $labels.instance }})"
          description: "..."
```

Nếu không sửa được rule thì dùng **label rules** trên UI — xem
[Gán nhãn](labelling.md).

## Ngưỡng và giá trị hiện tại

Card Teams hiển thị dòng `93% (ngưỡng 85%)` — nhưng **chỉ khi alerting rule
gửi kèm**.

**Alertmanager không gửi giá trị metric.** Payload webhook chỉ có `status`,
`labels`, `annotations`, `startsAt`, `endsAt`, `generatorURL`, `fingerprint`.
Không có trường nào chứa số đo. Cách duy nhất là Prometheus template nó vào
annotation.

```yaml
- alert: HighCPU
  expr: 100 - (avg by(instance,cluster) (rate(node_cpu_seconds_total{mode="idle"}[5m])) * 100) > 85
  for: 5m
  labels:
    severity: critical
    service: payments-api
    cluster: prod-hcm
  annotations:
    summary: "CPU cao trên {{ $labels.instance }}"
    value: '{{ printf "%.1f" $value }}%'     # giá trị hiện tại
    threshold: "85%"                          # ngưỡng, gõ tay cho khớp expr
    runbook_url: "https://wiki.company.local/runbooks/high-cpu"
```

### Ba điều phải biết

- **`{{ $value }}` chỉ dùng được trong `annotations`**, không dùng được trong
  `labels` — Prometheus đánh giá label trước khi có giá trị.
- Với alert có `for:`, `$value` là giá trị **tại lúc alert kích hoạt**, không
  phải lúc gửi thông báo.
- **`threshold` phải gõ tay.** Prometheus không suy ra được từ `expr`. Sửa
  `expr` mà quên sửa `threshold` là card nói dối — đó là cái giá của việc
  hiển thị ngưỡng.

### Tên key chấp nhận được

OpsTrail tra `annotations` trước, rồi `labels`:

| Ý | Key |
|---|---|
| Giá trị hiện tại | `value` · `current_value` · `current` · `observed` · `actual` · `metric_value` |
| Ngưỡng | `threshold` · `limit` · `expected` · `target` |

Giá trị hiện **nguyên văn** như rule viết — `1.2 GB` ra `1.2 GB`. OpsTrail
không parse số để format lại, vì đó là cách đơn vị bị làm hỏng.

Rule chưa có annotation thì card **vẫn gửi bình thường**, chỉ thiếu dòng đó.

## Định tuyến nhiều mức

Mỗi team một channel Teams riêng: khai báo team trong OpsTrail kèm Power
Automate URL của nó (**Labels → Teams**), rồi để Alertmanager gửi tất cả về
cùng một receiver. OpsTrail tự chọn channel theo nhãn `team` của alert.

Nếu muốn tách ngay từ Alertmanager:

```yaml
route:
  receiver: opstrail
  group_by: ['alertname', 'service']
  group_wait: 30s
  group_interval: 5m
  repeat_interval: 4h

  routes:
    - matchers: [ severity="critical" ]
      receiver: opstrail
      repeat_interval: 1h        # nhắc lại dày hơn cho P1

    - matchers: [ severity="info" ]
      receiver: opstrail
      group_interval: 30m        # gom lâu hơn, đỡ ồn
```

## Các tham số đáng chú ý

| Tham số | Ảnh hưởng tới OpsTrail |
|---|---|
| `group_by` | Quyết định bao nhiêu alert nằm chung một payload. OpsTrail lưu **từng** alert trong `alerts[]` thành một dòng riêng |
| `group_wait` | Độ trễ trước lần gửi đầu. Production nên 30s |
| `repeat_interval` | AM gửi lại alert chưa resolved. OpsTrail **UPSERT**, không tạo dòng mới, và **không** gửi lại card Teams |
| `resolve_timeout` | Sau bao lâu AM coi alert là hết nếu Prometheus ngừng gửi |

`repeat_interval` ngắn **không** làm Teams bị dội: OpsTrail chỉ gửi khi alert
**mới** hoặc **đổi trạng thái**.

```
lần 1    {"received":1,"notified":1,"queued":2}
lần 2-5  {"received":1,"notified":0,"queued":0}   ← repeat, im lặng
resolved {"received":1,"notified":1,"queued":2}   ← đổi trạng thái, có gửi
```

## Kiểm chứng

```bash
# qua Alertmanager thật
curl -X POST http://localhost:9093/api/v2/alerts \
  -H 'Content-Type: application/json' -d '[{
    "labels":{"alertname":"TestFromAM","severity":"warning",
              "service":"payments-api","team":"payments"},
    "annotations":{"summary":"kiem tra tich hop"},
    "startsAt":"'"$(date -u +%Y-%m-%dT%H:%M:%S.000Z)"'"}]'

# thang vao OpsTrail, bo qua Alertmanager
curl -X POST http://localhost:8000/api/v1/webhooks/alertmanager \
  -H 'Content-Type: application/json' \
  -H 'Authorization: Bearer <INTAKE_TOKEN>' \
  -d '{"version":"4","status":"firing","alerts":[{
       "status":"firing",
       "labels":{"alertname":"Direct","severity":"info"},
       "annotations":{"summary":"test"},
       "startsAt":"2026-10-06T10:00:00Z",
       "endsAt":"0001-01-01T00:00:00Z",
       "fingerprint":"test0001"}]}'
```

Phản hồi `{"received":N,"notified":M,"queued":K}`:

- `received` — số alert đã **lưu**
- `notified` — số alert mới hoặc đổi trạng thái (số card sẽ gửi)
- `queued` — số bản ghi delivery đã tạo

## Lỗi thường gặp

| Triệu chứng | Nguyên nhân | Cách sửa |
|---|---|---|
| `401` ở Alertmanager log | `INTAKE_TOKEN` lệch | So lại `credentials` trong `http_config` |
| `422` | Payload thiếu `fingerprint` hoặc `startsAt` | Kiểm tra `version: 4`; không dùng `webhook_config` quá cũ |
| Alert vào nhưng không có nhãn | Rule không gửi label `service`/`team` | Thêm label vào rule, hoặc tạo label rule trong OpsTrail |
| `ends_at` luôn rỗng | Thiếu `send_resolved: true` | Thêm vào receiver |
| Teams không nhận | `MSTEAMS_POWER_AUTOMATE_URL` sai | Xem bảng `notifications`, cột `last_error` |
| Mọi notification `ConnectError` | `.env` chứa URL `localhost` bị Compose đọc | Xem [Cấu hình](configuration.md) |

```sql
-- alert gan nhat da vao chua
SELECT id, alertname, severity, status, label_source, received_at
FROM alerts ORDER BY received_at DESC LIMIT 10;

-- gui di co loi khong
SELECT channel, status, attempts, response_status, left(last_error, 80)
FROM notifications ORDER BY id DESC LIMIT 10;
```

---

[← Về README](../README.md)
