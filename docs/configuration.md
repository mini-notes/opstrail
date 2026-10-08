# Cấu hình

Toàn bộ biến môi trường và cách cắm MS Teams.

## Cấu hình

| Env | Mặc định | |
|---|---|---|
| `DATABASE_URL` | `postgresql+asyncpg://...` | |
| `INTAKE_TOKEN` | *(rỗng)* | Bearer của Alertmanager. Rỗng = **tắt kiểm tra** |
| `MSTEAMS_POWER_AUTOMATE_URL` | *(rỗng)* | URL Power Automate |
| `MSTEAMS_TEMPLATE` | `msteams/default.json.j2` | |
| `OUTBOUND_WEBHOOK_ENABLED` | `false` | webhook other |
| `OUTBOUND_WEBHOOK_URL` / `_TOKEN` | | |
| `DISPATCH_GIVE_UP_AFTER_MINUTES` | `60` | |
| `ALERT_RETENTION_MONTHS` | `6` | `0` = tắt purge |
| `RETENTION_HOUR_UTC` | `3` | |

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

Thiếu cấu hình thì OpsTrail cảnh báo lúc khởi động và **vẫn gửi alert** (không
kèm header) — lỗi xác thực không được chặn alert.

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

### Sửa template

`app/templates/msteams/default.json.j2` xuất **thẳng Adaptive Card JSON**.
Mọi giá trị phải đi qua `| tojson` — đừng bao giờ viết `{{ value }}` trần bên
trong chuỗi JSON. Template hỏng sẽ fail ngay lúc render kèm nội dung sai,
không phải fail ở Power Automate.

Thử trước ở <https://adaptivecards.io/designer>.

---


---

[← Về README](../README.md)