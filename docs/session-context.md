# Context bàn giao phiên OpsTrail

Cập nhật: 28/10/2026, Asia/Ho_Chi_Minh. Mục đích: có thể tiếp tục trong chat khác hoặc sau khi clone repo mà không phụ thuộc lịch sử hội thoại.

## 1. Yêu cầu và quyết định đã xác nhận

- OpsTrail phục vụ nội bộ trước mắt; chưa yêu cầu cách ly nhiều tenant.
- Tải đỉnh người dùng nêu: dưới 100 message/giây. Các benchmark hiện đo alert event/giây; không mặc định đó là 100 webhook request/giây hoặc 100 Teams card/giây.
- Khi Teams phục hồi sau outage, tổng hợp sự kiện cũ thay vì phát lại toàn bộ card riêng.
- Triển khai bằng Docker Compose; PostgreSQL do team nội bộ quản lý.
- Củng cố OpsTrail chạy ổn định trước, rồi mới mở rộng tính năng. Daily health report là hướng sau, chưa triển khai.
- Người dùng đã cho phép commit/push phần hardening và hiện yêu cầu push tạm kế hoạch topology, UI demo và lưu context để làm sau.
- Chưa có yêu cầu triển khai discovery backend hoặc đưa UI demo vào ứng dụng production. Không tự tạo lịch/automation hoặc tiếp tục chạy thử nền.

## 2. Hardening đã hoàn tất phần local

Nhánh làm việc: `codex/harden-opstrail`. Remote hiện tại là repo `mini-notes/opstrail` trên GitHub. Không merge vào main.

Các commit trước đợt lưu kế hoạch này:

- `1abf4e8`: checkpoint WIP hardening trước khi người dùng tắt máy.
- `de586f6`: hoàn tất code hardening và kiểm chứng Compose/recovery local.

Đã thực hiện: bảo vệ template, atomic alert + notification transaction, immutable delivery snapshot, worker riêng với lease/token theo đích, retry và digest backlog, retention an toàn, readiness/metrics/heartbeat, Compose production, backup/restore scripts, CI và tài liệu.

Bằng chứng: **383 test pass, 0 skip**, 1 warning deprecation có sẵn. Bài tải 60.000 event/~600 giây ở 100 event/s, không lỗi HTTP; p95 66,9 ms và p99 122,7 ms. Đã đối soát 24.000 alert/42.000 delivery đúng kỳ vọng. Có smoke test batch 1/100, hai worker, pending qua restart và backup/restore mẫu.

Giới hạn: receiver là mock local, chưa kết nối Teams nội bộ hoặc triển khai production; pilot 7 ngày vẫn chờ hạ tầng/quyền/cấu hình thực tế. Không suy RTO production từ DB mẫu 10 alert. Counter receiver không ghi nhận duplicate key trong pilot, nhưng cơ chế không cam kết exactly-once.

Tài liệu authoritative:

- [Kết quả kiểm chứng](validation-hardening.md), [số liệu JSON](validation/hardening-results.json).
- [Runbook triển khai/vận hành](operations.md).
- [Review và kế hoạch củng cố](review-sre-2026-10-27.md).
- [Checkpoint hardening cũ](hardening-checkpoint.md) giữ để tham khảo lịch sử; danh sách outstanding cũ đã được kết quả kiểm chứng mới thay thế.

Các container thử nghiệm đã dừng, giữ dữ liệu. Docker context dùng được trong phiên trước: `desktop-linux`; không đổi context mặc định. Container PostgreSQL disposable: `opstrail-review-postgres`, port loopback 55432. Các tài nguyên/credentials test chỉ dùng local, không dùng cho production. Không tự khởi động lại nếu nhiệm vụ tiếp theo chỉ là nghiên cứu/tài liệu.

## 3. Ý tưởng topology do người dùng nêu

Người dùng có GitOps monitoring trên GitLab, sẽ khảo sát và cấp link/quyền đọc sau. Repo chứa scrape config và monitoring rules. Người dùng dự định chuẩn hóa service/team labels, phân loại infra/app/third-party và tổ chức monitoring rõ ràng hơn.

Mục tiêu: một view tổng hợp cho biết system, component, service/module nào đã được monitor; bằng cách nào; đang thu thập loại metric nào; có rule/check nào và còn thiếu gì.

**Chưa được đọc repo GitLab thật** và chưa có URL, branch, deployment mapping, collector/evaluator endpoints, phiên bản hay token của repo đó trong nhiệm vụ này. Không giả định dữ liệu demo phản ánh hạ tầng của người dùng.

## 4. Thiết kế đã đề xuất, chưa phải quyết định triển khai đã chốt

Chi tiết và nguồn nghiên cứu: [topology-discovery-proposal.md](topology-discovery-proposal.md).

- Kết hợp catalog kỳ vọng + GitOps declaration + runtime evidence. Chỉ discovery từ config sẽ không hiện hệ thống chưa được monitor.
- Phân biệt declared, loaded, observed và coverage theo requirement; không dùng một dấu xanh để đại diện tất cả.
- Ưu tiên coverage matrix; graph dùng để drill-down và xem provenance.
- Tách monitoring topology khỏi application dependency. Quan hệ depends_on cần khai báo hoặc bằng chứng traffic/tracing, không suy chỉ từ việc cùng xuất hiện trong PromQL.
- Định danh service logic khác deployment/target/job/IP. Rule và resource có quan hệ nhiều-nhiều; hỗ trợ recording-rule dependencies và PromQL label transformations.
- Giữ owner tài nguyên khác owner ứng dụng dùng tài nguyên. Đề xuất `team` số ít để khớp OpsTrail; ý nghĩa label `service` cần kiểm tra xung đột với Kubernetes Service trước khi áp dụng.
- Phân loại layer/management/kind thay vì coi infra/app/third-party luôn loại trừ nhau.
- Catalog/profile có thể nằm cùng GitOps hoặc tham chiếu nguồn authoritative khác. Không bắt buộc triển khai Backstage/graph database.
- Dùng rendered artifacts có provenance với Helm/Kustomize/Jsonnet; branch HEAD không mặc nhiên là commit đang deploy. Không chạy tùy ý script/plugin từ repo chỉ vì có quyền đọc.
- Scanner có worker, snapshot, last-good state và giới hạn tải riêng; không ảnh hưởng intake/dispatch. Chỉ đọc nguồn được cấp, không tự sửa rule/routing.

Những nội dung này là đề xuất cho khảo sát, chưa được người dùng duyệt thành schema, conventions hoặc tiêu chí nghiệm thu cuối cùng.

## 5. UI demo hiện có

Người dùng yêu cầu vài UI demo sinh động, xuất HTML để hình dung trước khi tự khảo sát/lên kế hoạch.

- [HTML độc lập](demos/monitoring-topology.html): tự chứa, mở offline, không cần server/package/network.
- [Hướng dẫn demo](demos/README.md).
- Bốn màn hình: Coverage, Topology, Khoảng trống, Chi tiết service.
- Có bộ lọc môi trường/owner, search, dialog evidence/YAML, entity drill-down và tình huống giả lập triển khai rule latency.
- Dataset giả lập gồm payments-api, orders-worker, payments-postgres, redis-cache, payment-provider, reporting-service. Topology minh hoạ tập trung hệ thống Payments.
- Coverage ban đầu 12/23 requirement; mô phỏng triển khai latency tăng lên 13/23. N/A không nằm trong mẫu số. Reporting là catalog-only để minh hoạ đối tượng chưa có monitor.
- Nét đứt dependency biểu thị khai báo, không xác nhận traffic. Không có thao tác deploy, sync GitLab hoặc gọi Prometheus thật.
- Đã kiểm tra cú pháp JS, HTML, ID và DOM mô phỏng cho các tương tác chính; chưa QA trực quan bằng browser automation vì browser chặn file://. Không tìm cách vượt chính sách browser.
- Bản inline của chat được tạo ngoài repo trong thư mục visualization của phiên. Bản HTML trong repo là deliverable di động để chỉnh sửa/tiếp tục; không cần đường dẫn visualization của máy cũ.
- Trạng thái preview gần nhất được nhận từ UI: trang Topology, monitoring pipeline, không lọc, selected payments-api, mô phỏng deploy đang tắt. Đây chỉ là trạng thái xem, không phải lựa chọn thiết kế đã được phê duyệt.

## 6. Cách tiếp tục

Đọc [plan topology](plans/topology-discovery.md), đề xuất chi tiết và mở demo. Bước kế tiếp là khảo sát repo/format/deployment trên phạm vi nhỏ, chốt identity/labels/catalog/profile rồi mới chia task thực thi và ước lượng.

Thông tin cần người dùng cung cấp qua cơ chế được cấp quyền: URL GitLab, repo/ref, mapping environment, thư mục config/rules, pipeline render/deploy và collector/evaluator/backend. Secret cấu hình qua secret/connector, không ghi vào chat hoặc tài liệu Git.

Không tự mở rộng daily health report vào scope topology. Không tự coi demo là UI production hoặc đánh dấu pilot thật hoàn tất.

## 7. Giữ riêng các thay đổi có sẵn

Các mục sau đã có trước việc thực hiện hardening/topology và chưa được xác nhận đưa vào commit kế hoạch này:

- `docs/opstrail-overview.html` đang sửa.
- `docs/opstrail-overview_old.html` chưa tracked.
- `plan_v2/` chưa tracked, là draft daily health report khác phạm vi.

Không git add toàn bộ repo, không ghi đè/xóa/đưa chúng vào commit nếu chưa có chỉ dẫn mới. Không commit env thật, token, dump, logs hoặc load-results runtime.
