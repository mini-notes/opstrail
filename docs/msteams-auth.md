# Xác thực khi gọi Power Automate

Mặc định OpsTrail gọi `MSTEAMS_POWER_AUTOMATE_URL` **không kèm header nào** —
URL chính là toàn bộ bí mật. Trang này nói cách bỏ tình trạng đó.

## 1. Vì sao URL thôi là không đủ — và vì sao gửi header thôi cũng không đủ

URL HTTP trigger của Power Automate mang chữ ký `sig=` ở query string. Ai có
URL là bắn được vào channel: không có mật khẩu, không có log phân biệt người
gửi, và xoay vòng khoá SAS là một quy trình thủ công qua DevTools trình duyệt.

Nhưng phải nói thẳng một điểm dễ nhầm:

> **Gửi thêm header từ OpsTrail không tự nó giải quyết gì.** Nếu flow vẫn để
> "Anyone", một request kèm header sai — hoặc không kèm header nào — vẫn chạy
> flow và vẫn đăng bài. Chốt chặn phải đặt ở **phía Power Automate**; OpsTrail
> chỉ là bên xuất trình giấy tờ.

Nên mỗi mức dưới đây gồm **hai nửa**: đặt biến ở OpsTrail, *và* sửa phía flow.
Làm một nửa là không bảo vệ được gì.

## 2. So sánh hai mức

| | Mức 1 — Entra ID OAuth | Mức 2 — header tĩnh |
|---|---|---|
| URL rò ra | Vô hại: URL không còn `sig=` | Vẫn gọi được flow, nhưng bị chặn bên trong |
| Chặn ở đâu | **Trước** khi flow chạy | **Sau** khi flow đã chạy (vẫn tốn lượt chạy) |
| Xoay vòng bí mật | Xoay client secret trong Entra | Sửa biến môi trường + sửa flow |
| Cần gì | App registration trong Entra ID | Không cần gì |
| `MSTEAMS_AUTH` | `entra` | `bearer` |

Chọn mức 1 nếu xin được app registration. Mức 2 là đường tạm, hoặc là chế độ
dùng khi có API gateway đứng trước Power Automate.

## 3. Mức 1 — Entra ID OAuth

### 3.1 Phía Power Automate

1. Mở flow → trigger **When a HTTP request is received**.
2. **Who can trigger the flow**: đổi từ *Anyone* sang
   *Specific users in my tenant*.
3. Lưu flow. **URL mới không còn `sig=`** — đó là dấu hiệu đã đổi đúng.
4. Ô **Allowed users**: dán **object ID của service principal**, nhiều giá trị
   phân cách bằng dấu `;`.

> **Bẫy 1 — object ID nào.** Phải lấy ở **Entra ID → Enterprise
> Applications** → tìm app → *Object ID*. **Không** phải object ID ở *App
> Registrations*: hai GUID khác nhau, và đây là chỗ nhầm phổ biến nhất. Nhầm
> thì flow trả **403** dù token hợp lệ.

### 3.2 Phía Entra ID

1. **App registrations** → *New registration*. Không cần redirect URI —
   OpsTrail dùng client credentials, không có người đăng nhập.
2. **Certificates & secrets** → *New client secret*. Chép giá trị ngay, sau
   đó Entra không cho xem lại.
3. Ghi lại **Directory (tenant) ID** và **Application (client) ID**.

### 3.3 Kiểm bằng `curl` trước khi cắm OpsTrail

Làm bước này trước, để nếu sai thì biết sai ở Entra hay ở OpsTrail:

```bash
curl -s -X POST \
  "https://login.microsoftonline.com/$ENTRA_TENANT_ID/oauth2/v2.0/token" \
  -d grant_type=client_credentials \
  -d client_id="$ENTRA_CLIENT_ID" \
  -d client_secret="$ENTRA_CLIENT_SECRET" \
  -d 'scope=https://service.flow.microsoft.com//.default'
```

> **Bẫy 2 — hai dấu gạch chéo.** Scope là
> `https://service.flow.microsoft.com//.default`. Thiếu một dấu `/` thì token
> vẫn lấy được nhưng Power Automate từ chối với `MisMatchingOAuthClaims` —
> lỗi hiện ở lúc gửi, không hiện ở lúc lấy token.

Có `access_token` rồi thì thử gọi thẳng flow:

```bash
curl -i -X POST "$MSTEAMS_POWER_AUTOMATE_URL" \
  -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"type":"message"}'
```

`202 Accepted` là xong. `401`/`403` thì xem mục 7.

### 3.4 Phía OpsTrail

```bash
MSTEAMS_AUTH=entra
ENTRA_TENANT_ID=<Directory (tenant) ID>
ENTRA_CLIENT_ID=<Application (client) ID>
ENTRA_CLIENT_SECRET=<client secret>
# ENTRA_SCOPE va ENTRA_TOKEN_URL de mac dinh khi dung Entra that
```

OpsTrail **cache token trong bộ nhớ** và làm mới 60 giây trước hạn, nên một
cơn bão alert không sinh ra hàng chục request token. Nếu Power Automate trả
401 hoặc 403, OpsTrail huỷ cache rồi thử lại **đúng một lần** — token hết hạn
giữa chừng tự sửa trong một nhịp, không phải chờ hết backoff.

## 4. Mức 2 — header tĩnh

### 4.1 Phía OpsTrail

```bash
MSTEAMS_AUTH=bearer
MSTEAMS_AUTH_TOKEN=<chuoi ngau nhien du dai, vi du openssl rand -hex 32>
# Doi ten header neu gateway doi khac, vi du X-Api-Key:
# MSTEAMS_AUTH_HEADER=X-Api-Key
# MSTEAMS_AUTH_SCHEME=            # de rong de gui thang gia tri, khong co "Bearer "
```

### 4.2 Phía flow — **bắt buộc**, không làm thì vô nghĩa

Ngay sau trigger, thêm một **Condition**:

- Bên trái: `triggerOutputs()?['headers']?['Authorization']`
- Phép so: *is equal to*
- Bên phải: `Bearer <đúng giá trị MSTEAMS_AUTH_TOKEN>`

Nhánh **If no** → action **Terminate** với status `Failed`. Nhánh **If yes**
mới đi tiếp đăng card.

**Giới hạn phải biết:** flow vẫn chạy trên **mọi** request, nên vẫn tốn lượt
chạy và vẫn bị dò. Khác biệt là kẻ có URL không đăng được bài.

## 5. Demo tại chỗ, không cần tenant

Stack dev có sẵn `mock-entra` — một endpoint `/token` giả, kiểm
`client_id`/`client_secret` và kiểm cả bẫy hai dấu gạch chéo. Nhờ nó chạy được
**toàn bộ** đường đi mà không cần Entra thật.

```bash
# .env
MSTEAMS_AUTH=entra
ENTRA_CLIENT_ID=mock-client-id
ENTRA_CLIENT_SECRET=mock-client-secret
ENTRA_TOKEN_URL=http://mock-entra:8081/token
```

```bash
docker compose up -d
```

`teams-mock` in lại toàn bộ header, nên thấy được `Authorization` tới nơi:

```bash
docker compose logs -f teams-mock | grep -i authorization
```

Đếm số lần lấy token — gửi vài alert mà chỉ thấy **1** thì cache đang đúng:

```bash
curl -s http://localhost:8081/stats
```

Thử sai mật khẩu để xem đường lỗi:

```bash
ENTRA_CLIENT_SECRET=sai docker compose up -d opstrail
docker compose logs opstrail | grep -i entra
```

Alert vẫn tới `teams-mock` — chỉ không kèm header, và log nói rõ vì sao. Đó là
chủ ý: **lỗi xác thực không được chặn alert**.

## 6. Xoay vòng bí mật

Client secret của Entra **có hạn** (mặc định 6 hoặc 24 tháng). Hết hạn là
alert ngừng tới Teams, và nó hỏng vào một ngày không ai nhớ.

- Đặt nhắc lịch **trước hạn 30 ngày**.
- Tạo secret mới **trước khi** xoá cái cũ — Entra cho phép hai secret cùng
  sống, nên đổi được không gián đoạn: thêm secret mới → đổi
  `ENTRA_CLIENT_SECRET` → khởi động lại OpsTrail → xoá secret cũ.
- Secret **chỉ** đi qua biến môi trường. Không vào DB, không vào `audit_log`,
  không bao giờ vào log — kể cả khi bật `DEBUG`.

## 7. Lỗi thường gặp

| Triệu chứng | Nguyên nhân |
|---|---|
| `MisMatchingOAuthClaims` | Scope thiếu một dấu `/`. Phải là `...flow.microsoft.com//.default` |
| **403** dù token lấy được | Dán object ID của *App Registrations* thay vì của *Enterprise Applications* |
| **401** sau một thời gian chạy tốt | Client secret hết hạn. Xem mục 6 |
| `invalid_client` / `AADSTS7000215` | Sai client secret, hoặc secret đã bị xoá |
| URL vẫn còn `sig=` | Chưa lưu flow sau khi đổi *Who can trigger* |
| Log khởi động cảnh báo thiếu cấu hình | `MSTEAMS_AUTH` đặt khác `none` mà thiếu biến. Alert **vẫn gửi**, chỉ không kèm header |

---

[← Về README](../README.md) · [Cấu hình](configuration.md) ·
[Template Teams](teams-template.md)
