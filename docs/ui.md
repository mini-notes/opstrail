# Giao diện quản trị

![Trang Alerts: sidebar, bộ lọc, bảng dữ liệu](images/alerts-list.png)

## Điều hướng

Sidebar trái, thu gọn được bằng nút `«` — trạng thái lưu `localStorage` và áp
**trước khi render** nên không nhảy layout. Dưới 860px sidebar thành lớp phủ,
mở bằng hamburger, đóng bằng Esc.

Mục **Unlabelled** mang badge số lượng alert đang chờ gán nhãn.

Thu gọn rồi chỉ còn dải biểu tượng, phần nội dung rộng thêm:

![Sidebar đã thu gọn](images/sidebar-collapsed.png)

| Trang | Có gì |
|---|---|
| `/alerts` | Danh sách, filter, sắp xếp, phân trang |
| `/alerts/{id}` | Chi tiết + lịch sử delivery + gán nhãn tay |
| `/unlabelled` | Backlog alert chưa ai gán nhãn, có nút `+ rule` |
| `/deliveries` | Mọi lần gửi: lọc theo channel, kết quả, service, team, thời gian |
| `/deliveries/{id}` | Một lần gửi: mã HTTP, số lần thử, lỗi đầy đủ, alert liên quan |
| `/dashboard` | 8 biểu đồ, lọc theo service/team/thời gian |
| `/labels/services` · `/labels/teams` · `/labels/rules` | Quản lý nhãn |
| `/settings/retention` | Hai cửa sổ retention, số sẽ xoá, nút chạy ngay |
| `/users` | Quản lý user — chỉ admin |
| `/docs` | Swagger của API |

Trang chi tiết gom bốn thứ người trực cần cùng lúc: nhãn đến từ đâu, mốc thời
gian, **lịch sử gửi từng kênh** (kèm mã HTTP và lỗi nếu có), và toàn bộ
label/annotation gốc. Khối *Set label by hand* ghi nhãn `manual` — Alertmanager
gửi lại cũng không ghi đè.

![Chi tiết alert: attribution, timing, delivery, labels](images/alert-detail.png)

`/alerts/{id}` **không phải route tuỳ chọn**: `app/notifiers/msteams.py` đã gắn
`detail_url = {public_host}/alerts/{id}` vào nút *Open in OpsTrail* trên mọi
Adaptive Card đã gửi. Đổi tên route là mọi card cũ trong Teams trỏ vào 404.

## Bảng dữ liệu

**Sắp xếp** — bấm vào tiêu đề cột, bấm lại để đổi chiều. Cột sắp xếp được khai
báo sẵn theo từng trang; giá trị lạ trong `?sort=` rơi về mặc định chứ không
lỗi. Tên cột **không bao giờ** lấy thẳng từ URL.

`severity` sắp theo **mức độ** (critical → error → warning → info) chứ không
theo bảng chữ cái. `duration` của alert đang firing là NULL nên xếp cuối.

![Sắp xếp theo severity: critical lên đầu](images/alerts-sort-severity.png)

Mọi thứ tự đều kết thúc bằng khoá chính. Thiếu khoá phụ này thì sắp theo cột
nhiều giá trị trùng kèm `LIMIT/OFFSET` sẽ làm một bản ghi xuất hiện ở hai
trang và một bản ghi khác biến mất.

![Thanh phân trang](images/alerts-pager.png)

**Số dòng** — chọn 25 / 50 / 100 ở góc phải thanh phân trang. Giá trị ngoài
danh sách rơi về mặc định. Đổi số dòng hoặc đổi cột sắp xếp đều quay về trang
1 và **giữ nguyên filter** đang áp.

## Theme sáng / tối

Nút ☀ / 🌙 / 🖥 trong dropdown tài khoản góc phải. Lần đầu theo cài đặt hệ
điều hành; chọn rồi thì ghi `localStorage`. Script đặt theme nằm inline trong
`<head>` nên không nháy trắng khi tải trang ở chế độ tối.

![Dashboard ở chế độ tối](images/dashboard-dark.png)

Biểu đồ **vẽ lại** khi đổi theme. Canvas không phải CSS: thứ đã vẽ giữ nguyên
màu lúc tạo, nên nếu không vẽ lại thì bấm "Light" xong chữ trên biểu đồ gần như
vô hình.

## Lịch sử gửi

Biểu đồ *Delivery outcome* trên dashboard nói **bao nhiêu** lần gửi hỏng.
`/deliveries` nói **lần nào**, và đầu bên kia trả lời gì.

![Trang Deliveries](images/deliveries.png)

Khoảng thời gian ở đây tính theo lúc **thử gửi**, không phải lúc alert bắt
đầu — khác có chủ ý với biểu đồ trên dashboard. Một lần thử lại hôm nay của
alert hai ngày trước vẫn thuộc "Last 1d", vì trang này nói về việc gửi.

Bấm vào dòng để mở chi tiết. Chi tiết là **trang riêng** chứ không phải dòng
xổ ra: `last_error` giữ tới 2000 ký tự, và khi template tuỳ chỉnh hỏng thì cả
output render nằm trong đó — thứ hữu ích nhất trên trang và không thể đọc
trong một ô bảng.

![Chi tiết một lần gửi](images/delivery-detail.png)

### URL webhook là bí mật

Cột *Target* luôn hiện dạng rút gọn. URL Power Automate đầy đủ **chính là**
thông tin xác thực — ai có nó là gửi được vào channel. Chỉ `admin` xem được
đầy đủ, và lần xem đó ghi vào audit log.

## Retention

![Trang Retention](images/retention.png)

Hai cửa sổ chạy mỗi ngày một lần, và trang này cho **chạy ngay** để khỏi phải
đợi tới 03:00 rồi đi đọc log. Số "sẽ xoá ở lần chạy tới" tính bằng đúng điều
kiện mà job dùng, nên nó là con số thật chứ không phải ước lượng.

Dòng **đang chờ gửi lại** không bao giờ bị xoá, dù cũ đến đâu.

## Dashboard

Tám biểu đồ, lọc chung theo service / team / khoảng thời gian. Mỗi biểu đồ trả
lời một câu hỏi vận hành cụ thể — *rule nào ồn nhất*, *nhãn đến từ đâu*, *còn
bao nhiêu alert chưa ai nhận*:

![Dashboard đầy đủ](images/dashboard-light.png)

Khối **How long alerts stay firing** ghi rõ **không phải MTTR**: đó là thời
gian điều kiện alert còn đúng. Rule flapping cắt một sự cố thành nhiều cặp
ngắn, nên MTTR thật cần bảng `incidents` ở Phase 2.

## Tài sản tĩnh: vendor, không CDN

HTMX và Chart.js nằm trong `app/static/vendor/`, commit thẳng vào repo. On-prem
thường chặn egress, và một dashboard trắng trơn vì CDN bị chặn là lỗi rất khó
đoán.

## Làm mới ảnh chụp trong tài liệu

Ảnh trong `docs/images/` chụp bằng `deploy/screenshots.py` — chạy tay sau khi
đổi giao diện, không nằm trong image và không có trong CI:

```bash
pip install playwright
OPSTRAIL_ADMIN_PASSWORD=... python deploy/screenshots.py
cd docs/images && for f in *.png; do sips --resampleWidth 1600 "$f"; done
```

---

[← Về README](../README.md)
