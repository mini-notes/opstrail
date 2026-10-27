# Cấu hình

Toàn bộ biến môi trường và cách cắm MS Teams.

## Cấu hình

| Env | Mặc định | |
|---|---|---|
| `DATABASE_URL` | `postgresql+asyncpg://...` | |
| `INTAKE_TOKEN` | *(rỗng)* | Bearer của Alertmanager. Rỗng = **tắt kiểm tra trong development**; production bắt buộc token riêng ≥32 ký tự |
| `MSTEAMS_POWER_AUTOMATE_URL` | *(rỗng)* | URL Power Automate |
| `MSTEAMS_TEMPLATE` | `msteams/default.json.j2` | |
| `OUTBOUND_WEBHOOK_ENABLED` | `false` | webhook other |
| `OUTBOUND_WEBHOOK_URL` / `_TOKEN` | | |
| `ENVIRONMENT` | `development` | Production phải đặt `production`; từ chối cấu hình secret/auth không an toàn |
| `SESSION_SECRET` | giá trị dev | Production cần secret riêng tối thiểu 32 ký tự |
| `PUBLIC_HOST` | `http://localhost:8000` | Production phải là HTTPS; dùng kiểm tra Origin và link UI |
| `OPS_METRICS_TOKEN` | rỗng | Token riêng cho `/metrics`, tối thiểu 32 ký tự ở production |
| `RUN_BACKGROUND_TASKS` | `true` | Đặt `false` khi API dùng worker chuyên dụng |
| `DISPATCH_CONCURRENCY` | `8` | Số lane worker, 1..32; lease vẫn serialize một đích |
| `DISPATCH_TIMEOUT_SECONDS` | `15` | Timeout HTTP; toàn bộ auth/send có trần 3 lần giá trị này |
| `DISPATCH_LEASE_SECONDS` | `120` | Ít nhất 4 lần HTTP timeout + 10 giây |
| `DISPATCH_TARGET_INTERVAL_SECONDS` | `1` | Nhịp gửi tối thiểu theo đích; hiệu chỉnh theo quota thật |
| `DISPATCH_BASE_BACKOFF_SECONDS` / `DISPATCH_MAX_BACKOFF_SECONDS` | `5` / `300` | Backoff có jitter, Retry-After có thể dài hơn |
| `BACKLOG_DIGEST_AFTER_SECONDS` | `300` | Event chờ quá cửa sổ này được đưa vào digest |
| `BACKLOG_DIGEST_MAX_EVENTS` | `100` | Tối đa 1..100 event/phần; còn có giới hạn kích thước |
| `BACKLOG_RETENTION_HOURS` | `168` | Kết thúc retry nếu quá tuổi và lần thử vẫn lỗi |
| `DISPATCH_GIVE_UP_AFTER_MINUTES` / `DISPATCH_MAX_ATTEMPTS` | deprecated | Chỉ giữ tương thích env; worker mới không sử dụng |
| `ALERT_RETENTION_MONTHS` | `6` | `0` = tắt purge |
| `NOTIFICATION_RETENTION_MONTHS` | `3` | Lịch sử gửi. Chỉ có tác dụng khi **ngắn hơn** `ALERT_RETENTION_MONTHS` — xoá alert là các dòng gửi đi theo bằng CASCADE. Đặt bằng hoặc dài hơn thì cảnh báo lúc khởi động |
| `RETENTION_HOUR_UTC` | `3` | Chỉ scheduler demo; worker riêng kiểm tra overdue 24 giờ và chạy bù khi startup |
| `BUSINESS_TIMEZONE` | `Asia/Ho_Chi_Minh` | Múi giờ **của sàn**, dùng để đọc khung giờ trong severity rule. **Không phải** múi giờ hiển thị — cái đó theo máy từng người. Tên zone sai thì cảnh báo và rơi về UTC |

> Mọi mốc thời gian lưu trong DB là **UTC**. Múi giờ chỉ áp ở lớp hiển thị
> và ở ranh giới bucket biểu đồ — xem [ui.md](ui.md#múi-giờ).

### Xác thực khi gọi Power Automate

Mặc định OpsTrail gọi URL Power Automate **không kèm header nào** — URL là toàn
bộ bí mật. Đổi `MSTEAMS_AUTH` để thêm giấy tờ, nhưng **phải sửa cả phía flow**,
nếu không thì không bảo vệ được gì: [msteams-auth.md](msteams-auth.md).

| Env | Mặc định | |
|---|---|---|
| `MSTEAMS_AUTH` | `none` | `none` \| `bearer` \| `entra` |
| `MSTEAMS_AUTH_TOKEN` | *(rỗng)* | Bí mật tĩnh cho `bearer` |
| `MSTEAMS_AUTH_HEADER` | `Authorization` | Đổi khi gateway đòi header khác |
| `MSTEAMS_AUTH_SCHEME` | `Bearer` | Rỗng = gửi thẳng giá trị, không có tiền tố |
| `ENTRA_TENANT_ID` | *(rỗng)* | Directory (tenant) ID |
| `ENTRA_CLIENT_ID` | *(rỗng)* | Application (client) ID |
| `ENTRA_CLIENT_SECRET` | *(rỗng)* | Chỉ qua env. Không vào DB, không vào log |
| `ENTRA_SCOPE` | `https://service.flow.microsoft.com//.default` | **Hai** dấu `/` — thiếu một dấu là `MisMatchingOAuthClaims` |
| `ENTRA_TOKEN_URL` | *(rỗng)* | Rỗng = dựng từ tenant ID. Đặt tay để trỏ `mock-entra` |

Trong development, thiếu cấu hình auth được cảnh báo và có thể gửi không kèm header. Production từ chối cấu hình auth thiếu lúc startup. Lỗi lấy token trong runtime được ghi nhận và theo dõi qua delivery/retry.

---

## MS Teams thật

1. <https://make.powerautomate.com> → Create → Instant cloud flow
2. Trigger **When a Teams webhook request is received**
3. Action **Post card in a chat or channel** → Post as *Flow bot* → chọn channel
4. Save → copy **HTTP POST URL** → `MSTEAMS_POWER_AUTOMATE_URL`
5. `docker compose up -d --force-recreate opstrail`

Office 365 connector (`*.webhook.office.com`) đã bị Microsoft khai tử.
OpsTrail **từ chối** URL đó kèm thông báo rõ thay vì gửi vào hư không.

Mỗi team có thể có channel riêng: `teams.msteams_webhook_url`, fallback về
biến global.

Xem [runbook production](operations.md) cho Compose, giám sát, backup/restore và pilot.

### Sửa template

`app/templates/msteams/default.json.j2` xuất **thẳng Adaptive Card JSON**.
Mọi giá trị phải đi qua `| tojson` — đừng bao giờ viết `{{ value }}` trần bên
trong chuỗi JSON. Template hỏng sẽ fail ngay lúc render kèm nội dung sai,
không phải fail ở Power Automate.

Thử trước ở <https://adaptivecards.io/designer>.

---


---

[← Về README](../README.md)
Template tùy chỉnh chạy trong immutable sandbox, chỉ editor/admin được preview. Không dùng globals, mutation (`append`), macro/import hay vòng lặp lồng nhau. Giới hạn source 32 KiB, context/output 256 KiB; chỉ truyền dữ liệu thuần thay ORM object. Template lưu cũ có cấu trúc bị chặn sẽ fallback về built-in khi tạo snapshot mới.
