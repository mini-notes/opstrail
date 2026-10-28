# Kế hoạch tiếp theo: Monitoring Topology & Coverage

Trạng thái: **draft để khảo sát/làm sau**, chưa triển khai backend. Ngày: 28/10/2026.

- [Context phiên và quyết định đã xác nhận](../session-context.md).
- [Phân tích thiết kế và nguồn tham khảo](../topology-discovery-proposal.md).
- [Demo HTML offline](../demos/monitoring-topology.html).

## Mục tiêu sản phẩm

Biết đối tượng nào cần được monitor, Git đã khai báo gì, runtime thực sự có gì và thiếu requirement nào. Có provenance về file/commit/source/evidence để người vận hành kiểm tra được kết luận.

## Thứ tự triển khai đề xuất

| Bước | Việc cần làm | Đầu ra / điều kiện đi tiếp |
|---|---|---|
| D0 — Khảo sát | Nhận repo/ref/quyền đọc; khảo sát scrape, rule, template, pipeline và deployment mapping | Một sơ đồ dữ liệu thực tế và danh sách format hỗ trợ/chưa hỗ trợ; chưa viết parser dựa trên phỏng đoán |
| D1 — Chốt conventions | Identity system/service/resource/deployment, team ownership, labels/aliases, catalog authoritative và 2–3 profile | Schema và vài ví dụ được người dùng duyệt; xử lý rõ resource dùng chung và third-party |
| D2 — Git inventory MVP | Parse một repo/environment, lưu snapshot/provenance, UI coverage declared-only | Không bỏ qua lỗi format im lặng; catalog-only vẫn xuất hiện; link nguồn tới SHA cụ thể |
| D3 — Runtime reconcile | Một collector/evaluator; target, rule, metric evidence, freshness và drift | Phân biệt khai báo/nạp/quan sát; kiểm thử source outage, selector sai, rule chưa nạp và stale data |
| D4 — Coverage policy | Requirement/profile, findings, exceptions, diff giữa snapshot/commit | Mọi kết luận có scope và bằng chứng; N/A/Unknown rõ; rule chung không gán sai ownership |
| D5 — Mở rộng | Nhiều nguồn/environment, HA dedup, alert overlay, CI checks và báo cáo | Discovery nằm trong ngân sách tải; không đổi routing production tự động |

Chưa chốt ngày công hoặc kiến trúc chi tiết trước D0. Ưu tiên matrix hữu ích và evidence đúng trước graph lớn hoặc dependency discovery bằng tracing.

## Phạm vi pilot đề xuất

Một environment với: HTTP API, DB dùng chung, third-party probe, một rule qua recording rule, dynamic target và một entity cố tình chưa có monitoring. Chọn scope sau khi đọc repo thật.

## Các quyết định còn mở

- Prometheus thuần hay Operator/Helm/Kustomize/Jsonnet; có rendered artifact/source map không?
- Collector/evaluator/storage là những thành phần nào, phiên bản và API đọc được tới đâu?
- Nguồn catalog nào authoritative? Service/component/module được định nghĩa ở cấp nào?
- Quy ước nhãn nào có thể chuẩn hóa mà không va chạm nghĩa label đang có?
- Profile/coverage là requirement nào, freshness bao lâu, ngoại lệ do ai duyệt?
- Repo/ref nào tương ứng với từng environment đã triển khai?

## Không nằm trong MVP hiện tại

Tự sửa monitoring config/routing, tự suy dependency chỉ từ rule, quét mọi time series liên tục, graph database bắt buộc, AI quyết định coverage, daily health report hoặc thay thế CMDB/APM toàn diện.
