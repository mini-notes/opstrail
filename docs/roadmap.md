# Hiện trạng & lộ trình

> Số liệu chốt ngày **2026-10-06**. Cách tự kiểm lại nằm ở cuối trang — đừng
> tin con số ở đây nếu code đã đi tiếp một thời gian.

## 1. Đang có gì

| | |
|---|---|
| Code ứng dụng | 3.749 dòng Python |
| Template | 1.191 dòng HTML/Jinja |
| Test | 1.218 dòng, **131 test pass** |
| Migration | 3 |
| Bảng trong DB | 9 |
| Stack | Python 3.13 · FastAPI · SQLAlchemy 2 (async) · PostgreSQL 16 · Jinja2 + HTMX + Chart.js |

### Tính năng và cách đã kiểm chứng

| Tính năng | Kiểm chứng |
|---|---|
| Nhận alert từ Alertmanager | Chạy `prom/alertmanager` **thật** trong compose, bắn qua `amtool`/API |
| UPSERT `(fingerprint, starts_at)` | AM gửi lại 6 lần theo `repeat_interval` → vẫn 1 dòng |
| `send_resolved` | `ends_at` điền đúng, tính được duration 42 phút |
| Chỉ notify khi mới / đổi trạng thái | 5 lần gửi giống hệt → 1 alert, 2 notification |
| Lưu trước gửi sau | Tắt Teams, bắn alert → vẫn lưu; bật lại → tự `sent` |
| Retry theo tuổi | Giữ `pending` qua 75s, hồi phục sau khi Teams quay lại |
| Gán nhãn 3 bước | Alert có label → `payload`; không có → `rule`; không khớp → NULL |
| `payload` thắng `rule` | Dựng bẫy `job=node-exporter` + rule `priority=1` → ra `payload` |
| Nhãn tay sống sót repeat | Gán tay rồi bắn lại → không bị rule ghi đè |
| Backfill | Preview 17 (16 rule A + 1 rule B), đối chiếu SQL khớp, apply 16 |
| Wildcard | `pg-prod-*` khớp đúng; `100%` là literal |
| RBAC | viewer/editor/admin, 403 đúng chỗ |
| Chống tự khoá | Tự hạ vai trò / tự tắt → 400 |
| Phân trang ổn định | Sort `severity` (nhiều trùng), 19 trang × 25 → 463 ID phân biệt, không lặp không thiếu |
| Retention | Seed 21 cũ + 10 mới → purge đúng 21 |

### Chưa làm

- Sửa service/team/rule đã tạo (hiện chỉ thêm và xoá)
- Trang xem `audit_log` trên UI (đã ghi, chưa có trang)
- Giới hạn dữ liệu theo team (user chỉ thấy alert của team mình)
- REST API cho label_rules (hiện chỉ có form HTML)

---

## 2. Quyết định và lý do

Phần này để người đọc sau không phải đoán. Đặc biệt là những lần **đảo chiều**.

### Viết mới thay vì sửa một công cụ có sẵn

Các công cụ nhận alert sẵn có đều gắn chặt với phần của riêng chúng — agent
thu thập log, tích hợp AI, hàng loạt datasource — trong khi thứ cần ở đây là
bốn việc hẹp: nhận webhook, gán nhãn, lưu lịch sử, gửi Teams. Cộng thêm RBAC,
quản lý nhãn, dashboard và retention đều phải tự làm. Tỉ lệ bỏ đi so với giữ
lại không đáng để kế thừa, nên viết mới và chọn đúng stack mà team đọc được.

**Hai quyết định kỹ thuật đi kèm:**
- Đọc nhãn từ `alerts[].labels` chứ không chỉ `commonLabels` — đó là nơi
  Alertmanager đặt nhãn của từng alert riêng lẻ. Có regression test.
- Template Jinja2 xuất **thẳng** JSON Adaptive Card, không đi vòng qua
  markdown rồi convert. Markdown không diễn đạt được FactSet hay Action.

### Bỏ `passlib`

Hỏng hẳn với `bcrypt` 5.x — đọc `bcrypt.__about__` đã bị xoá rồi raise ở mọi
lần hash. Thư viện phát hành lần cuối 2020. Dùng `bcrypt` trực tiếp, pre-hash
SHA-256 + base64 để mật khẩu > 72 byte không bị cắt âm thầm.

### Dashboard: tự vẽ thay vì dựa vào Grafana

Ban đầu định trỏ Grafana vào Postgres để khỏi viết code biểu đồ. Khi xác nhận
Grafana không khả dụng trong môi trường đích thì chuyển sang Chart.js đóng gói
sẵn trong repo — đổi lại OpsTrail không phụ thuộc hệ thống ngoài nào để hiển
thị số liệu của chính nó.

### `label_rules`: fallback → cơ chế chính

Ban đầu thiết kế làm fallback, giả định nhãn `service`/`team` sẽ được thêm
thẳng vào alerting rule. Giả định đó không luôn đúng — rule Prometheus thường
do nhóm khác quản lý, hoặc có hàng trăm rule cũ không ai muốn đụng. Nên rule
gán nhãn trở thành cơ chế chính, kéo theo: UI quản lý rule, gợi ý label key từ
dữ liệu thật, wildcard, và **backfill** (vì alert đã resolved không bao giờ
được Alertmanager gửi lại).

### Hoãn DORA sang Phase 2

DORA có 4 metric, **3 trong 4 không đến từ alert** (Deployment Frequency, Lead
Time, Change Failure Rate đều cần dữ liệu CI/CD + git). Phase 1 chỉ làm alert.
Cái giá: Phase 2 phải thêm bảng `deployments` và backfill.

Đã để sẵn seam để giảm đau: `alerts.incident_id`, `alerts.assignee_id`,
`users.upn`, và bảng `incidents`.

---

## 3. Bẫy đã biết

Gom một chỗ vì mỗi cái đều tốn thời gian để tìm ra.

| Bẫy | Hậu quả |
|---|---|
| **`payload` thắng `rule`**, `job` nằm trong danh sách key tự động | Service key trùng tên scrape job → rule không bao giờ chạy |
| **Docker Compose tự đọc `.env`** | URL `localhost` trong `.env` đè service name → mọi notification `ConnectError` dù mạng thông |
| **"thời gian firing" ≠ MTTR** | Alert flapping cắt một sự cố thành nhiều cặp ngắn. MTTR thật cần bảng `incidents` |
| **Alembic mặc định không so `server_default`** | Thêm `server_default` vào model mà không migrate → `INSERT` bằng SQL thuần fail NOT NULL. Đã bật `compare_server_default=True` |
| **Postgres `GROUP BY` ưu tiên cột đầu vào** | `group_by("severity")` gom theo `alerts.severity` thô → `critical` và `CRITICAL` thành 2 lát biểu đồ |
| **Sort không tiebreaker** | Sắp theo cột nhiều giá trị trùng + `LIMIT/OFFSET` → một bản ghi ở 2 trang, một bản ghi biến mất |
| **Canvas không phải CSS** | Đổi theme mà không vẽ lại biểu đồ → chữ gần như vô hình |
| **Office 365 connector đã chết** | URL `*.webhook.office.com` không dùng được. OpsTrail từ chối kèm thông báo rõ |

---

## 4. Các phase tiếp

Xếp theo giá trị / chi phí.

### Phase 2

| | Việc | Seam đã có | Ghi chú |
|---|---|---|---|
| 2.1 | Sửa service/team/rule đã tạo; trang audit log | — | Nhỏ, nên làm trước |
| 2.2 | Mention đúng user trong Teams | `users.upn` | Adaptive Card `msteams.entities`. Cần UPN thật → đây là lúc OIDC Entra ID đáng giá |
| 2.3 | Assign alert cho user | `alerts.assignee_id` | Thêm UI assign, filter "của tôi" |
| 2.4 | Gom alert → incident | bảng `incidents`, `alerts.incident_id` | **Nền cho MTTR.** Gom theo service + cửa sổ thời gian, hoặc khai báo tay |
| 2.5 | DORA | cần thêm bảng `deployments` | `POST /api/v1/deployments` cho CI/CD; `commit_sha` + GitLab/GitHub API cho Lead Time; link incident ↔ deployment cho Change Failure Rate |
| 2.6 | MCP đọc Loki/Splunk/Prometheus | — | Python có SDK MCP chính thức. Tool **read-only**; đề xuất rule để người duyệt, không tự apply vào Prometheus |

### Phase 3

| Việc | Ghi chú |
|---|---|
| SSO Entra ID | Auth layer đã thiết kế pluggable. Lấy `upn` tự động, bỏ quản lý user tay |
| Giới hạn dữ liệu theo team | Thêm `users.team_id`, lọc ở mọi truy vấn |
| HA nhiều replica | `file` storage không share được; Postgres đã sẵn sàng. Cần xem lại dispatch sweeper để 2 replica không gửi trùng |
| Partition bảng `alerts` theo tháng | Khi vượt ~100k alert/tháng; purge thành `DROP PARTITION` |

**Thứ tự đề xuất:** 2.1 → 2.4 → 2.2 → 2.3 → 2.5 → 2.6.
Làm 2.4 (incident) trước 2.5 (DORA) vì MTTR phải tính trên incident.

---

## 5. Tự kiểm lại số liệu

```bash
find app -name '*.py' | xargs wc -l | tail -1          # code
find app/templates -name '*.html' | xargs wc -l | tail -1
find tests -name '*.py' | xargs wc -l | tail -1
.venv/bin/python -m pytest tests/ -q | tail -2          # so test
ls alembic/versions/*.py | wc -l                        # migration
docker compose exec postgres psql -U opstrail -d opstrail -c "\dt"
```

---

[← Về README](../README.md)
