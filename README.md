# OpsTrail

Nhận alert từ **Alertmanager**, gán nhãn service/team, lưu lịch sử vào
**PostgreSQL**, gửi sang **MS Teams** (Power Automate) và webhook tuỳ ý.

Không AI, không on-call. Phase 1.

![Dashboard OpsTrail](docs/images/dashboard-dark.png)

---

## 1. Chạy

```bash
cp .env.example .env            # sửa INTAKE_TOKEN + SESSION_SECRET
docker compose up -d
curl localhost:8000/healthz
```

Stack gồm 5 container: `postgres`, `opstrail`, **`alertmanager` thật**,
`teams-mock` (đóng vai Power Automate khi bạn chưa có flow — xem card nó nhận
bằng `docker compose logs -f teams-mock`) và `mock-entra` (endpoint token giả,
chỉ dùng khi demo [xác thực](docs/msteams-auth.md)). Migration tự chạy lúc
khởi động.

### ⚠️ Hai file env, đừng nhầm

| File | Ai đọc | URL dùng |
|---|---|---|
| `.env` | **docker compose** nội suy `${VAR}` | service name (`http://teams-mock:8080`) |
| `.env.local` | **pydantic** khi chạy uvicorn trực tiếp trên host | `http://localhost:8080` |

Docker Compose **tự động** đọc `.env` ở thư mục gốc. Nếu để URL `localhost`
trong đó, container sẽ POST vào chính nó và **mọi notification fail với
`ConnectError`** dù mạng hoàn toàn thông. Đây là lý do pydantic được trỏ sang
`.env.local` (xem `app/config.py`).

## 2. Tạo tài khoản

Bảng `users` khởi đầu rỗng:

```bash
docker compose exec opstrail python -m app.cli create-user \
  --username admin --role admin
```

Mở <http://localhost:8000/> và đăng nhập.

## 3. Cắm Alertmanager

```yaml
receivers:
  - name: opstrail
    webhook_configs:
      - url: http://opstrail:8000/api/v1/webhooks/alertmanager
        send_resolved: true          # BẮT BUỘC — không có thì ends_at luôn rỗng
        http_config:
          authorization:
            type: Bearer
            credentials: <INTAKE_TOKEN>
```

Nên gửi kèm label `service` và `team` trong alerting rule:

```yaml
- alert: PostgresqlDown
  expr: up{job="postgres"} == 0
  for: 2m
  labels:
    severity: critical
    service: payments-api     # khớp services.key trong OpsTrail
    team: payments            # khớp teams.key
```

Không sửa được rule? Tạo **label rule** trên UI — xem
[Gán nhãn](docs/labelling.md).

→ Chi tiết: **[Tích hợp Alertmanager](docs/alertmanager.md)** (định tuyến
nhiều mức, group_by, repeat_interval, lỗi thường gặp).

## 4. Cắm MS Teams

1. <https://make.powerautomate.com> → Create → Instant cloud flow
2. Trigger **When a Teams webhook request is received**
3. Action **Post card in a chat or channel** → Post as *Flow bot* → chọn channel
4. Save → copy **HTTP POST URL** → `MSTEAMS_POWER_AUTOMATE_URL` trong `.env`
5. `docker compose up -d --force-recreate opstrail`

Office 365 connector (`*.webhook.office.com`) đã bị Microsoft khai tử. OpsTrail
**từ chối** URL đó kèm thông báo rõ thay vì gửi vào hư không.

## 5. Kiểm chứng

```bash
curl -X POST http://localhost:9093/api/v2/alerts \
  -H 'Content-Type: application/json' -d '[{
    "labels":{"alertname":"TestFromAM","severity":"warning",
              "service":"payments-api","team":"payments"},
    "annotations":{"summary":"kiem tra tich hop"},
    "startsAt":"'"$(date -u +%Y-%m-%dT%H:%M:%S.000Z)"'"}]'
```

Rồi kiểm:

- Alert hiện ở <http://localhost:8000/alerts>
- `docker compose logs -f teams-mock` có Adaptive Card
- Trang chi tiết alert, mục **Delivery**, trạng thái `sent`

## Chạy không cần Docker

```bash
python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"
cp .env.local.example .env.local
.venv/bin/alembic upgrade head
.venv/bin/uvicorn app.main:app --reload
```

---

## Tài liệu

| | |
|---|---|
| [Tích hợp Alertmanager](docs/alertmanager.md) | Định tuyến, group_by, xác thực, lỗi thường gặp |
| [Gán nhãn service/team](docs/labelling.md) | 3 bước gán nhãn, label rule, wildcard, backfill |
| [Metadata & owner](docs/metadata.md) | Khai báo team/service/owner, @mention trong Teams, quy trình test |
| [Lịch sử gửi & retention](docs/ui.md#lịch-sử-gửi) | Trang Deliveries, chi tiết một lần gửi, hai cửa sổ retention |
| [Xác thực Power Automate](docs/msteams-auth.md) | Entra ID OAuth hoặc header tĩnh, demo tại chỗ, xoay vòng bí mật |
| [**Hướng dẫn tạo template Teams**](docs/teams-template-howto.md) | Từng bước từ con số không tới card dùng được, kèm ví dụ copy được |
| [Template card Teams](docs/teams-template.md) | Sửa template trên UI, biến dùng được, hai lớp bảo vệ |
| [Giao diện quản trị](docs/ui.md) | Điều hướng, bảng dữ liệu, sắp xếp, theme |
| [Quản trị](docs/administration.md) | User, RBAC, audit log, retention |
| [Cấu hình](docs/configuration.md) | Toàn bộ biến môi trường |
| [Kiến trúc](docs/architecture.md) | Data model, luồng xử lý, dashboard |
| [Hiện trạng & lộ trình](docs/roadmap.md) | Đang ở đâu, các phase tiếp theo |
| [Tổng quan (HTML)](docs/opstrail-overview.html) | Sơ đồ luồng, giải thích cơ chế — mở trực tiếp bằng trình duyệt |

## Xử lý sự cố nhanh

| Triệu chứng | Xem |
|---|---|
| Alertmanager báo 401 / 422 | [alertmanager.md](docs/alertmanager.md#lỗi-thường-gặp) |
| Alert vào nhưng không có nhãn | [labelling.md](docs/labelling.md) |
| Mọi notification `ConnectError` | Mục "Hai file env" phía trên |
| `ends_at` luôn rỗng | Thiếu `send_resolved: true` |
| Teams không nhận card | `SELECT channel, status, last_error FROM notifications ORDER BY id DESC LIMIT 10;` |
| Mention không hoạt động | [metadata.md](docs/metadata.md#5-lỗi-thường-gặp) |
| Teams trả 401/403, hoặc `MisMatchingOAuthClaims` | [msteams-auth.md](docs/msteams-auth.md#7-lỗi-thường-gặp) |

## Test

```bash
.venv/bin/python -m pytest tests/ -q
.venv/bin/ruff check app/ tests/
```

Xem `NOTICE` cho giấy phép của các thư viện đóng gói kèm.
