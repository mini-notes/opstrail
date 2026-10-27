# Kết quả kiểm chứng hardening — 28/10/2026

Trạng thái: phần triển khai và kiểm chứng local đã hoàn tất, có thể chuẩn bị pilot có kiểm soát. Chưa xác nhận vận hành production hoặc pilot 7 ngày trên hạ tầng/Teams nội bộ.

Số liệu máy đọc được: [hardening-results.json](validation/hardening-results.json). Runbook: [operations.md](operations.md).

## Kiểm thử và image

- **383 passed, 0 skipped**, một cảnh báo deprecation có sẵn của Starlette TestClient/httpx. PostgreSQL 16 thật, DB test cô lập; CI bắt buộc DB và thất bại nếu integration suite bị skip do DB không khả dụng.
- Ruff và kiểm tra whitespace pass; shell scripts qua `sh -n`.
- Image build thành công với runtime dependencies đã pin, `pip check` không báo lỗi. API/worker chạy UID 10001, root filesystem read-only, cap_drop ALL, memory/CPU limit trong Compose.
- Migration từ DB rỗng, downgrade một revision rồi upgrade lại được kiểm chứng; `alembic check` không phát hiện lệch schema.
- Production Compose được kiểm tra cấu trúc. API readiness, metrics bearer auth, Origin, config fail-fast và worker health được kiểm chứng.

## Tải intake

| Kịch bản | Event | HTTP request | Lỗi HTTP | p95 | p99 |
|---|---:|---:|---:|---:|---:|
| 100 event/s, batch 10, ~600 giây; outage rồi recovery | 60.000 | 6.000 | 0 | 66,9 ms | 122,7 ms |
| 100 event/s, batch 1, ~30 giây; receiver khỏe | 3.000 | 3.000 | 0 | 104,4 ms | 340,7 ms |
| 100 event/s, batch 100, 30 request; receiver khỏe | 3.000 | 30 | 0 | 394,5 ms | 397,9 ms |

Bài 10 phút đạt khoảng 100,01 event/s theo elapsed monotonic, tạo đúng **24.000 alert / 42.000 delivery** theo workload firing/repeat/resolved. Bài batch 1 tạo đúng 3.000/3.000; batch 100 tạo đúng 1.020/2.010. Đối soát DB theo run_id không thiếu snapshot và không có digest member mồ côi.

Benchmark đặt batch/episode độc lập giữa các request để tránh producer tự đảo firing/resolved. Một lượt chạy ngay lúc recreate API đã gặp lỗi cold start và không được tính vào bảng pass; harness đã thêm chờ readiness trước khi tính tải. Khi rollout một host, gián đoạn ngắn vẫn tồn tại và Alertmanager cần retry.

Môi trường local Docker Desktop/aarch64: API giới hạn 1 CPU/512 MiB; mỗi worker 1 CPU/512 MiB; PostgreSQL trong Docker VM. Mẫu quan sát trong bài tải: API khoảng 76 MiB, worker khoảng 67 MiB; đây là mẫu tức thời, không phải đỉnh RAM hay capacity cam kết. Phút đầu bài dài có chạy integration test trên DB khác cùng PostgreSQL container. Bài dài chạy trước các chỉnh sửa cuối về telemetry/retention; smoke test batch 1/100 dùng image cuối.

100 event/s intake không đại diện 100 Teams card/s. Pilot dùng receiver disposable, window digest **5 giây**, backoff tối đa 10 giây và interval đích **0,05 giây** để tăng tốc diễn tập; mặc định production vẫn window 300 giây, interval 1 giây. Cần hiệu chỉnh theo quota thật.

## Outage, concurrency và restart

- Receiver bắt đầu trả 503, sau đó được bật 200. Worker gửi backlog thành các digest cố định, không phát lại từng card cũ.
- Hai worker cùng chạy healthy; healthcheck theo hostname container và lease/token được kiểm chứng. Receiver mô phỏng theo dõi delivery key và không ghi nhận key trùng trong các lượt pilot này.
- Đã tạo thêm 8 pending delivery, restart cả hai worker rồi khôi phục receiver; đối soát sau restart vẫn đủ 4 alert/8 delivery, không thiếu snapshot hoặc mất liên kết.
- Integration tests bao gồm atomic rollback, request trùng concurrent, token cũ trước/trong send, timeout tổng, Retry-After, không gộp digest lồng, phạm vi team/giới hạn nhóm, requeue và retention anchor/member.

Không tuyên bố exactly-once: test lease hết hạn trong lúc HTTP đã được nhận cho thấy request có thể lặp lại với cùng key; receiver hỗ trợ dedup mới loại được bản lặp. Token fencing chỉ ngăn chủ lease cũ ghi đè trạng thái DB. Webhook có Idempotency-Key; Teams header là dấu truy vết, không phải cam kết dedup của Microsoft.

## Backup/restore

Dùng pg_dump/pg_restore 16 trong container, dữ liệu mẫu 10 alert/10 notification và schema ab1200261027. Backup khoảng **1,10 giây**, restore sang DB mới khoảng **1,08 giây**, archive 52.871 byte. Schema version, số bản ghi và checksum toàn bộ row của cả alerts/notifications khớp giữa nguồn và bản restore.

Bài này chứng minh script và đường phục hồi local. Chưa chứng minh RPO/RTO với dung lượng production, backup off-host, chính sách mã hóa/lưu giữ hoặc quyền tài khoản nội bộ.

## Điều kiện trước pilot thật

1. Cấp VM/Compose host, DSN/role/CA của PostgreSQL, DNS/TLS và secret/Teams flow theo chính sách nội bộ; cấu hình qua secret/env tại máy đích, không gửi secret vào chat.
2. Gán owner cho backup/off-host copy, cảnh báo độc lập và xử lý sự cố của chính OpsTrail.
3. Chạy smoke end-to-end trên flow thật; xác nhận card hiển thị, scope team, quota, digest và chính sách retry.
4. Theo dõi ít nhất 7 ngày theo runbook, thực hiện restore với dữ liệu đủ lớn và chấp thuận RPO/RTO trước khi vận hành chính thức.
