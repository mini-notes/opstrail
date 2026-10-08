# Giao diện quản trị

## Điều hướng

Sidebar trái, thu gọn được bằng nút `«` — trạng thái lưu `localStorage` và áp
**trước khi render** nên không nhảy layout. Dưới 860px sidebar thành lớp phủ,
mở bằng hamburger, đóng bằng Esc.

Mục **Unlabelled** mang badge số lượng alert đang chờ gán nhãn.

| Trang | Có gì |
|---|---|
| `/alerts` | Danh sách, filter, sắp xếp, phân trang |
| `/alerts/{id}` | Chi tiết + lịch sử delivery + gán nhãn tay |
| `/unlabelled` | Backlog alert chưa ai gán nhãn, có nút `+ rule` |
| `/dashboard` | 8 biểu đồ, lọc theo service/team/thời gian |
| `/labels/services` · `/labels/teams` · `/labels/rules` | Quản lý nhãn |
| `/users` | Quản lý user — chỉ admin |
| `/docs` | Swagger của API |

`/alerts/{id}` **không phải route tuỳ chọn**: `app/notifiers/msteams.py` đã gắn
`detail_url = {public_host}/alerts/{id}` vào nút *Open in OpsTrail* trên mọi
Adaptive Card đã gửi. Đổi tên route là mọi card cũ trong Teams trỏ vào 404.

## Bảng dữ liệu

**Sắp xếp** — bấm vào tiêu đề cột, bấm lại để đổi chiều. Cột sắp xếp được khai
báo sẵn theo từng trang; giá trị lạ trong `?sort=` rơi về mặc định chứ không
lỗi. Tên cột **không bao giờ** lấy thẳng từ URL.

`severity` sắp theo **mức độ** (critical → error → warning → info) chứ không
theo bảng chữ cái. `duration` của alert đang firing là NULL nên xếp cuối.

Mọi thứ tự đều kết thúc bằng khoá chính. Thiếu khoá phụ này thì sắp theo cột
nhiều giá trị trùng kèm `LIMIT/OFFSET` sẽ làm một bản ghi xuất hiện ở hai
trang và một bản ghi khác biến mất.

**Số dòng** — chọn 25 / 50 / 100 ở góc phải thanh phân trang. Giá trị ngoài
danh sách rơi về mặc định. Đổi số dòng hoặc đổi cột sắp xếp đều quay về trang
1 và **giữ nguyên filter** đang áp.

## Theme sáng / tối

Nút ☀ / 🌙 / 🖥 trong dropdown tài khoản góc phải. Lần đầu theo cài đặt hệ
điều hành; chọn rồi thì ghi `localStorage`. Script đặt theme nằm inline trong
`<head>` nên không nháy trắng khi tải trang ở chế độ tối.

Biểu đồ **vẽ lại** khi đổi theme. Canvas không phải CSS: thứ đã vẽ giữ nguyên
màu lúc tạo, nên nếu không vẽ lại thì bấm "Light" xong chữ trên biểu đồ gần như
vô hình.

## Tài sản tĩnh: vendor, không CDN

HTMX và Chart.js nằm trong `app/static/vendor/`, commit thẳng vào repo. On-prem
thường chặn egress, và một dashboard trắng trơn vì CDN bị chặn là lỗi rất khó
đoán.

---

[← Về README](../README.md)
