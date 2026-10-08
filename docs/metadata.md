# Metadata: team, service, owner

Metadata là thông tin OpsTrail gắn vào alert để biết **cái gì hỏng, của ai,
báo cho ai**. Có ba chiều:

| Chiều | Để làm gì | Ảnh hưởng tới gửi tin? |
|---|---|---|
| **Service** | Đơn vị dịch vụ. Gốc của mọi thống kê theo dịch vụ | Gián tiếp — quyết định team và owner |
| **Team** | Nhóm sở hữu. Có thể có channel Teams riêng | **Có** — `msteams_webhook_url` của team |
| **Owner** | Người chịu trách nhiệm | **Có** — được @mention khi alert đủ nặng |

> Alert **vẫn gửi bình thường** khi chưa khai báo gì. Metadata là phụ gia:
> không có owner thì card không có mention, thế thôi. Không bao giờ vì thiếu
> metadata mà alert bị chặn.

---

## 1. Khai báo

Thứ tự nên làm: **Teams → Services → Owners → gán owner**.

### Teams

**Labels → Teams → Thêm team**

| Trường | |
|---|---|
| `key` | Đem so với label `team` / `squad` / `owner` của alert |
| Tên | Hiển thị trên card và dashboard |
| Power Automate URL | *(tuỳ chọn)* channel riêng của team; để trống thì dùng channel chung |

### Services

**Labels → Services → Thêm service**

| Trường | |
|---|---|
| `key` | Đem so với label `service` / `app` / `component` / **`job`** của alert |
| Team | Alert của service này kế thừa team đó |
| Type | `api` / `worker` / `db`… dùng để phân loại trên dashboard |

> **⚠️ Đừng đặt `key` trùng tên scrape job** trừ khi bạn thực sự muốn thế.
> `job` nằm trong danh sách key tự động và bước `payload` **thắng** mọi label
> rule — chi tiết ở [Gán nhãn](labelling.md).

### Owners

**Labels → Owners → Thêm owner**

Owner **không cần tài khoản OpsTrail**. Đây là danh bạ riêng.

| Trường | Bắt buộc? | Ghi chú |
|---|---|---|
| Tên | ✓ | Hiện trong `<at>Tên</at>`, nên để đúng tên trên Teams |
| Email | nên có | Dùng làm `mentioned.id` mặc định |
| UPN | nên có | Thường trùng email |
| AAD Object ID | tuỳ | Dự phòng khi mention bằng UPN không đẩy được thông báo |
| Điện thoại | tuỳ | Để sẵn cho SMS ngoài giờ ở phase sau |

#### Lấy UPN / AAD Object ID ở đâu

```bash
# Azure CLI
az ad user show --id a.nguyen@company.com --query "{upn:userPrincipalName, id:id}" -o json
```

Hoặc Azure Portal → Microsoft Entra ID → Users → chọn người →
**Object ID** và **User principal name**.

### Gán owner cho service / team

Trên trang **Services** hoặc **Teams**, chọn owner và vai trò:

| Vai trò | Ý nghĩa |
|---|---|
| `primary` | Người đầu tiên được mention |
| `backup` | Dự phòng |
| `escalation` | Dành cho leo thang ở phase sau |

---

## 2. Mention hoạt động thế nào

Khi một alert đến, OpsTrail:

1. Lấy owner của **service**; service không có owner thì lấy owner của **team**.
   **Không gộp cả hai** — service đã có người phụ trách riêng mà vẫn ping cả
   team là cách nhanh nhất để người ta tắt thông báo.
2. Tìm ngưỡng severity: **service → team → mặc định toàn cục**.
3. Nếu severity của alert **nặng bằng hoặc hơn** ngưỡng → chèn mention.

So sánh theo **bậc** (critical=1 … info=4), không theo bảng chữ cái — xếp
alphabet thì `critical` nằm dưới `warning`, ngược hoàn toàn.

### Đặt ngưỡng

| Nơi | Giá trị |
|---|---|
| `teams.mention_severity` | mặc định `critical` |
| `services.mention_severity` | để trống = kế thừa team |
| `MENTION_SEVERITY_DEFAULT` | khi cả hai đều trống |

```sql
-- service core: mention từ mức error trở lên
UPDATE services SET mention_severity = 'error' WHERE key = 'payments-api';
```

### Card trông thế nào

```
🔥 FIRING: PostgresqlDown
<at>Nguyen Van A</at> <at>Le Thi B</at> — cần xử lý
Postgres down (instance pg-prod-01)

Severity   🔴 CRITICAL
Service    Payments API
Team       Payments
Owner      Nguyen Van A, Le Thi B
Instance   pg-prod-01
Started    2026-10-07 09:00:00 UTC

[📖 Runbook] [🔍 Prometheus] [🗂 OpsTrail] [🔕 Silence]
```

Dòng `Owner` hiện **kể cả khi không mention** — biết ai phụ trách mà không
làm phiền họ lúc nửa đêm.

---

## 3. Quy trình test mention

Làm tuần tự, mỗi bước có cách kiểm.

### Bước 1 — tạo owner là chính bạn

**Labels → Owners**, nhập tên đúng như trên Teams, email và UPN là của bạn.

### Bước 2 — gán vào một service thử

**Labels → Services** → chọn service → gán owner, vai trò `primary`.

```sql
-- kiểm tra đã gán chưa
SELECT s.key, o.name, so.role, s.mention_severity, t.mention_severity AS team_threshold
FROM service_owners so
JOIN owners o   ON o.id = so.owner_id
JOIN services s ON s.id = so.service_id
LEFT JOIN teams t ON t.id = s.team_id;
```

### Bước 3 — bắn alert critical

```bash
curl -X POST http://localhost:9093/api/v2/alerts \
  -H 'Content-Type: application/json' -d '[{
    "labels":{"alertname":"MentionTest","severity":"critical","service":"<service_key>"},
    "annotations":{"summary":"kiem tra mention"},
    "startsAt":"'"$(date -u +%Y-%m-%dT%H:%M:%S.000Z)"'"}]'
```

### Bước 4 — kiểm card trước khi đụng Teams thật

```bash
docker compose logs teams-mock | grep -A5 '"entities"'
```

Phải thấy:

```json
"entities": [
  { "type": "mention",
    "text": "<at>Nguyen Van A</at>",
    "mentioned": { "id": "a.nguyen@company.com", "name": "Nguyen Van A" } }
]
```

Và trong `body` có một `TextBlock` chứa **đúng chuỗi** `<at>Nguyen Van A</at>`.
Hai chỗ lệch nhau một ký tự là Teams hiện ra chữ `<at>...</at>` thô.

Bắn thêm một alert `warning` cùng service — **không được** có `entities`.

### Bước 5 — thử trên Teams thật

Đổi `MSTEAMS_POWER_AUTOMATE_URL` sang flow thật rồi bắn lại alert critical.

| Kết quả | Nghĩa là | Làm gì |
|---|---|---|
| Tên hiện xanh **và** có thông báo đẩy | ✅ xong | |
| Tên hiện xanh nhưng **không** rung chuông | `mentioned.id` chưa đúng định dạng tenant | Điền AAD Object ID, đặt `MSTEAMS_MENTION_ID_FIELD=aad`, thử lại |
| Hiện chữ `<at>Tên</at>` thô | `text` trong entities và TextBlock lệch | Kiểm template, hoặc về mặc định |
| Không có mention nào | Chưa đạt ngưỡng, hoặc owner chưa có định danh | Xem bảng chẩn đoán dưới |

---

## 4. Cấu hình

| Biến | Mặc định | |
|---|---|---|
| `MSTEAMS_MENTION_ID_FIELD` | `upn` | `upn` dùng email/UPN · `aad` dùng AAD Object ID |
| `MENTION_SEVERITY_DEFAULT` | `critical` | Khi service và team đều không đặt ngưỡng |

Owner thiếu định danh được chọn thì **tự lùi** sang cái còn lại
(`upn → email → aad` hoặc ngược lại), nên danh bạ điền dở vẫn mention được
chứ không im lặng.

---

## 5. Lỗi thường gặp

| Triệu chứng | Nguyên nhân | Cách sửa |
|---|---|---|
| Không có mention nào | severity chưa đạt ngưỡng | `SELECT mention_severity FROM services WHERE key='…'` |
| | service và team đều chưa có owner | Gán owner |
| | owner đang bị tắt | Bật lại ở trang Owners |
| | owner không có email/UPN/AAD | Điền ít nhất một cái |
| Mention sai người | service có owner riêng, team có owner khác | Service owner thắng — đúng thiết kế |
| Hiện `<at>Tên</at>` thô | template tự sửa đặt mention ngoài TextBlock/FactSet | Chỉ hai loại đó hiển thị được mention |
| Card hiện tên nhưng không báo | `mentioned.id` sai định dạng | Đổi `MSTEAMS_MENTION_ID_FIELD` |

```sql
-- owner nào chưa có định danh để mention
SELECT id, name FROM owners
WHERE is_active AND upn IS NULL AND email IS NULL AND aad_object_id IS NULL;

-- service nào chưa có ai phụ trách
SELECT s.key FROM services s
LEFT JOIN service_owners so ON so.service_id = s.id
WHERE so.owner_id IS NULL;
```

---

[← Về README](../README.md) · [Gán nhãn](labelling.md) · [Template Teams](teams-template.md)
