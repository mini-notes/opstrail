# OpsTrail Monitoring Discovery — UI demo

Mở `monitoring-topology.html` trực tiếp trong trình duyệt. File tự chứa HTML/CSS/JavaScript, không cần cài package, server hoặc kết nối mạng.

Đây là bản mẫu để thảo luận sản phẩm, không phải tính năng đã triển khai vào OpsTrail. Toàn bộ catalog, commit, metric, rule và runtime evidence là dữ liệu giả lập.

## Bốn góc nhìn

- **Coverage:** matrix entity × signal; bấm tên để mở entity, bấm ô để xem bằng chứng.
- **Topology:** chuyển giữa monitoring pipeline và dependency khai báo của hệ thống Payments; chọn node để xem scope. Bộ lọc chỉ giữ node/cạnh phù hợp.
- **Khoảng trống:** danh sách finding có owner và đường vào evidence.
- **Chi tiết service:** profile, chuỗi catalog → Git → runtime và metric family minh hoạ.

## Tình huống tương tác

1. Mở Coverage, bấm ô Latency của `payments-api`: rule có trong Git, runtime chưa nạp.
2. Bấm “Mô phỏng triển khai”: trạng thái chuyển sang có evidence, coverage tăng từ 12/23 lên 13/23 yêu cầu.
3. Mở Topology, xem cạnh rule latency đổi từ nét đứt sang nét liền.
4. Chọn owner Analytics: `reporting-service` vẫn xuất hiện trong catalog dù chưa có monitoring declaration.
5. Mở dependency: PostgreSQL là resource do Platform sở hữu; quan hệ phụ thuộc được ghi rõ là khai báo, không khẳng định đã thấy traffic.

Bản HTML không gọi GitLab/Prometheus, không triển khai rule và không sửa dữ liệu thật. “Xem GitOps/YAML” mở ví dụ local. Coverage là phép tính từ các ô minh hoạ, không phải điểm đánh giá hệ thống thực tế.

## Kiểm chứng

Đã kiểm tra cú pháp JavaScript và mô phỏng DOM: bốn view, bộ lọc/search, dialog evidence, drilldown, cập nhật/reset scenario, graph scope và loại bỏ cạnh mồ côi. HTML parse được và không có tài nguyên mạng ngoài.

Chưa QA trực quan bằng browser automation: browser của phiên làm việc chặn giao thức file://. Không thay đổi chính sách browser hoặc mở nguồn thật để vượt chặn. Cần xem trực tiếp trên desktop/mobile trước khi chuyển bản mẫu thành UI ứng dụng.

## Tài liệu tiếp tục

- [Plan topology](../plans/topology-discovery.md)
- [Context bàn giao phiên](../session-context.md)
- [Phân tích chi tiết](../topology-discovery-proposal.md)
