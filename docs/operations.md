# Vận hành OpsTrail bằng Docker Compose

Phạm vi: một hệ thống nội bộ, PostgreSQL do team quản lý, API và worker dùng cùng image. Compose một host chưa cung cấp HA. Bản kiểm chứng local và điều kiện pilot được ghi trong [validation-hardening.md](validation-hardening.md).

## 1. Chuẩn bị cấu hình

- PostgreSQL 16 có DB ứng dụng và cơ chế backup ngoài máy chủ. Team DB cấp kết nối, CA/TLS và tài khoản phù hợp chính sách nội bộ.
- Runtime cần SELECT/INSERT/UPDATE/DELETE và quyền dùng sequence; migration cần DDL. Sau migration, team DB phải cấp quyền trên các bảng/sequence mới cho runtime. Có thể dùng file env migration riêng qua `OPSTRAIL_MIGRATION_ENV_FILE`.
- Reverse proxy nội bộ phục vụ HTTPS, chuyển tiếp về `127.0.0.1:8000`. Mẫu: [nginx.conf.example](../deploy/nginx.conf.example). Nếu proxy chạy trong container, cấu hình mạng upstream tương ứng; loopback của container không phải host.
- Tạo `.env.production` từ `.env.production.example`, đặt quyền `0600`, thay ba secret độc lập và DSN/URL Teams thật. Tạo secret bằng `python -c 'import secrets; print(secrets.token_urlsafe(48))'`.
- Production từ chối secret yếu/mẫu, PUBLIC_HOST không phải HTTPS và cấu hình auth thiếu. ENVIRONMENT phải là `production`; `RUN_BACKGROUND_TASKS=false` cho API và worker chuyên dụng.
- Không đưa env thật, URL có token hoặc dump lên Git. Cấu hình Entra có thêm client secret nếu flow yêu cầu.
- `OPSTRAIL_PORT` mặc định 8000; `OPSTRAIL_BIND_ADDRESS` mặc định 127.0.0.1. Chỉ đổi bind address khi đã có firewall phù hợp.

Compose production dùng DB được cấp qua DSN, không tự tạo DB/mock. Chạy bằng `-f compose.production.yml`; không gộp với demo `docker-compose.yml`.

## 2. Rollout và migration

```sh
# Chọn image tag bất biến theo release/commit; giữ tag cũ để rollback.
export OPSTRAIL_IMAGE=opstrail:YOUR_RELEASE
# Khi build tại máy đích; có thể dùng image từ registry thay thế.
docker compose -f compose.production.yml build
# Nếu tài khoản migration riêng:
# export OPSTRAIL_MIGRATION_ENV_FILE=/secure/path/migration.env
# Sao lưu DB trước rollout theo quy trình của team.
docker compose -f compose.production.yml run --rm migrate
# Chỉ chạy bước này khi migration thành công.
docker compose -f compose.production.yml up -d api worker
docker compose -f compose.production.yml ps
curl --fail http://127.0.0.1:8000/readyz
```

Compose recreate API có gián đoạn ngắn; Alertmanager phải được cấu hình retry và `send_resolved: true`. Bài benchmark chờ `/readyz` trước khi tính tải; cold start được kiểm tra riêng.

Trong script tự động dùng `set -e` để lỗi migration chặn rollout. Không khởi động API/worker bằng `up -d` tất cả services rồi mong migration tự đóng vai trò gate. `migrate` là job thực thi riêng trước rollout.

Tạo admin bằng CLI tương tác: `docker compose -f compose.production.yml exec api python -m app.cli create-user --username admin --role admin`. Mật khẩu không truyền qua command line.

Migration `ab1200261027` chỉ thêm bảng/cột và index. Delivery cũ không có snapshot lịch sử: worker ghi snapshot khi claim lần đầu hoặc báo trạng thái Unknown trong digest. Các notification đã failed từ release cũ không tự gửi lại hàng loạt; xem xét rồi dùng lệnh requeue có chọn lọc.

## 3. Luồng gửi và tổng hợp sau outage

- Intake commit alert và notification snapshot trong cùng transaction; lỗi rollback toàn bộ để Alertmanager gửi lại.
- Repeat cùng trạng thái không tạo card mới. Các request concurrent cùng identity được serialize ở DB. OpsTrail xử lý theo thứ tự nhận/commit; webhook đến sai thứ tự từ upstream vẫn cần điều tra tại nguồn.
- Worker giữ lease theo đích, gửi ngoài transaction, ghi kết quả từng delivery và fence completion theo claim token. Trần toàn bộ thời gian auth/send nhỏ hơn lease; HTTP timeout riêng lẻ không thay thế trần này.
- `DISPATCH_TARGET_INTERVAL_SECONDS=1` là nhịp khởi đầu theo đích, cần chỉnh theo quota Power Automate thực tế. 100 event/s intake không có nghĩa gửi được 100 card/s vào Teams.
- Backoff có jitter, tôn trọng Retry-After hợp lệ (giới hạn tối đa 24 giờ). Lỗi 4xx vĩnh viễn thành failed; 401/403, 408/409/425/429 và lỗi tạm thời tiếp tục thử trong cửa sổ backlog.
- Event chờ quá `BACKLOG_DIGEST_AFTER_SECONDS` (mặc định 300) được nhóm theo channel, URL đích và team. Mỗi phần tối đa 100 event, có giới hạn kích thước; một outage lớn có thể tạo nhiều phần.
- Snapshot digest và danh sách delivery thành viên được commit trước khi gửi. Retry không nhận thêm thành viên mới. Digest đang chờ không bị lồng vào digest khác.
- Bản tổng hợp mô tả trạng thái **tại snapshot của nhóm**, không khẳng định đó là trạng thái sống ngay lúc người nhận đọc. Phần trước được gửi trước các root delivery mới hơn cùng đích. Với nhiều phần, một episode có thể xuất hiện ở hai phần với hai thời điểm; UI lưu lịch sử đầy đủ.
- Teams card hiển thị tối đa 20 mục chi tiết để giữ gọn; payload/delivery IDs còn lại xem ở trang Deliveries. Webhook digest có schema `opstrail.digest.v1`.
- Delivery nguồn có `digest_parent_id` được xem là gửi qua bản tổng hợp; không có HTTP call riêng. `sent` nghĩa endpoint chấp nhận, cần kiểm chứng flow để biết card đã hiện trong Teams.

Generic webhook nhận `Idempotency-Key` ổn định qua retry; Teams nhận `X-OpsTrail-Delivery-Id` để truy vết. Receiver cần tự hỗ trợ dedup mới có tác dụng chống lặp phía đích. Crash sau khi đích nhận nhưng trước commit vẫn có khoảng mơ hồ; không cam kết exactly-once.

Khi worker bị kill giữa send, giữ nguyên lease đến hết hạn để tránh phát lại quá sớm. Không xóa lease thủ công chỉ để tăng tốc phục hồi, trừ khi đã xác nhận process cũ ngừng và hiểu khả năng gửi lặp.

## 4. Giám sát và xử lý lỗi

- `/healthz`: liveness process.
- `/readyz`: DB truy vấn được trong timeout; Teams lỗi không làm API mất readiness.
- `/metrics`: yêu cầu `Authorization: Bearer <OPS_METRICS_TOKEN>` riêng; không dùng intake token. Chỉ scrape từ mạng được phép, lưu token trong secret/file cấu hình Prometheus.
- `opstrail_intake_requests_total{status_class}` và `opstrail_intake_duration_seconds`: counter/histogram intake, gồm auth/validation/server error. Chạy một uvicorn process mỗi API container; scale bằng container và scrape từng instance. Counter reset khi restart.
- `opstrail_deliveries{channel,status}`: số row theo trạng thái, gồm delivery nguồn của digest.
- `opstrail_pending_oldest_age_seconds`: tuổi root pending lâu nhất, kể cả root bị thiếu next_attempt.
- `opstrail_worker_last_success_timestamp_seconds{component}`: dispatch heartbeat, maintenance và retention thành công gần nhất. Heartbeat chứng minh vòng worker còn chạy; backlog age phát hiện không tiến triển.
- Worker healthcheck đọc heartbeat theo hostname container, tránh một worker khỏe che worker khác bị treo.
- Mẫu alert: [prometheus-alerts.yml](../deploy/prometheus-alerts.yml). Định tuyến lỗi OpsTrail/Teams qua kênh độc lập, không gửi duy nhất về chính OpsTrail/Teams.

Nếu pending tăng: kiểm tra worker health/log, DB, DNS/TLS, HTTP status, quota và target. Không scale worker để vượt quota một đích. Xem Deliveries để biết digest gốc/thành viên. Log và lỗi ghi DB không nên chứa toàn bộ secret URL.

Nếu failed: sửa nguyên nhân trước, xem payload/đích rồi requeue có chọn lọc:

```sh
docker compose -f compose.production.yml exec api python -m app.cli requeue-delivery --id 123
```

Lệnh chỉ nhận delivery failed, giữ nguyên snapshot/lịch sử thời gian và ghi audit. Với thành viên digest, requeue parent. Không đổi tất cả failed thành pending bằng SQL: có thể gửi lại nội dung đã được nhận, trộn phạm vi hoặc tạo tải lớn. Delivery quá tuổi backlog sau requeue sẽ được thử, nhưng nếu còn lỗi thì kết thúc lại; chỉ requeue sau khi đã sửa nguyên nhân.

Retention giữ mọi firing và pending. Alert resolved tính tuổi từ ends_at, fallback updated_at; pending liên quan chặn xóa alert. Worker chuyên dụng kiểm tra retention khi startup và chạy khi lần thành công trước quá 24 giờ; không phụ thuộc việc process có sống đúng giờ cron hay không. Chế độ demo in-process vẫn dùng scheduler cũ.

## 5. Backup và restore

Backup là trách nhiệm cần gán owner cụ thể với team DB; volume không phải backup. Thống nhất RPO/RTO, nơi lưu ngoài host, mã hóa, quyền đọc và lịch xóa. Mẫu hiện không tự xóa dump và chưa cung cấp PITR; cần theo dõi dung lượng. Đề xuất ban đầu RPO 24h/RTO 2h phải được xác nhận bằng dữ liệu thật.

```sh
# .env.backup chứa PGHOST/PGDATABASE/PGUSER và PGPASSFILE hoặc PGPASSWORD.
# Không commit credentials. Nếu dùng PGPASSFILE phải mount secret read-only.
export OPSTRAIL_BACKUP_DIR=/secure/backups/opstrail
docker compose -f compose.production.yml --profile backup up -d backup
```

Mẫu backup dùng postgres:16-alpine, custom-format dump, quyền file hạn chế và đổi tên `.partial` sau khi kiểm tra archive. Healthcheck báo unhealthy khi quá khoảng 25 giờ chưa có dump mới; Docker không tự restart chỉ vì unhealthy, nên cần giám sát container độc lập và đưa bản sao ra ngoài host. Không dựa vào metrics heartbeat tên backup: API hiện không tự đọc trạng thái backup container.

Diễn tập restore bằng client PostgreSQL cùng major với DB, vào DB **mới** kết thúc `_restore_test`:

```sh
PGHOST=db.internal PGUSER=restore_operator PGDATABASE=opstrail_drill_restore_test \
  PGPASSFILE=/secure/pgpass sh deploy/restore-test.sh /secure/backups/opstrail/example.dump
```

Script không DROP/--clean và dừng nếu DB đã tồn tại. So sánh schema version, số bản ghi, notification snapshot/digest membership và đăng nhập UI trên môi trường cô lập. Role runtime, ownership, grants, extensions hoặc tablespaces ngoài schema ứng dụng cần team DB phục hồi riêng vì dump dùng `--no-owner --no-acl`. Đo thời gian với dung lượng production, không suy từ DB mẫu nhỏ.

## 6. Rollback và pilot

Giữ tag image cũ và backup trước rollout. Khi rollback: dừng worker mới để không chạy lẫn hai cơ chế dispatch khác nhau; dừng API mới nếu cần; xác nhận schema tương thích rồi mới bật image cũ. Release trước hardening có lỗ hổng template và dispatcher không lease, nên không dùng làm phương án chạy production dài hạn. Không downgrade migration khi còn pending digest: sẽ mất snapshot/membership; ưu tiên sửa tiến hoặc restore có kiểm soát nếu thật sự cần.

Pilot thật tối thiểu 7 ngày sau khi cấu hình DB/TLS/Teams nội bộ được cấp:

| Giai đoạn | Kiểm chứng |
|---|---|
| Ngày 1 | Nhận firing/repeat/resolved thật; RBAC/Origin; card hiện đúng trên Teams; owner vận hành rõ |
| Ngày 2 | Ngắt receiver thử nghiệm, khôi phục; digest đúng phạm vi; không replay hàng loạt card cũ |
| Ngày 3 | Restart worker/API có kế hoạch; lease hết hạn và tiếp tục đúng payload |
| Ngày 4 | Rate limit/Retry-After, lỗi auth, một đích hỏng không chặn đích khác |
| Ngày 5 | Backup off-host và restore dữ liệu đủ lớn; đo RPO/RTO |
| Ngày 6 | Quan sát backlog, DB/CPU/RAM/storage; hiệu chỉnh quota/ngưỡng |
| Ngày 7 | Review failure/audit, kiểm tra rollback và quyết định vận hành chính thức |

Bài load test chỉ chạy trên DB/mạng receiver disposable. `deploy/load_test.py` giới hạn URL localhost, lấy token từ OPSTRAIL_TEST_TOKEN, kiểm tra expected delivery count và lưu JSON. Dùng batch 1/10/100 để phân biệt HTTP requests với alert events. Không dùng receiver Teams thật để benchmark.

Sau load test, đối soát read-only bằng `DATABASE_URL=... PYTHONPATH=. python deploy/verify_load.py load-results/run.json`; DB phải có hậu tố `_test`. Kiểm tra số alert/delivery, snapshot và digest member không mồ côi. Retention giữ anchor digest tới khi các member đã được purge để liên kết UI không bị đứt.
