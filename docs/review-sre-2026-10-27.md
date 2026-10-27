# Review OpsTrail và kế hoạch cải thiện

Ngày: 27/10/2026. Phạm vi: tài liệu, source, cấu hình triển khai và test trong checkout hiện tại. Đây là review và kế hoạch; chưa thay đổi ứng dụng, kết nối nguồn metrics hay gửi thông báo.

## 1. Kết luận

OpsTrail có phạm vi Phase 1 hợp lý: nhận Alertmanager → gán service/team → lưu PostgreSQL → gửi Teams/webhook, kèm UI quản trị. Stack FastAPI + PostgreSQL + HTML/HTMX phù hợp một công cụ nội bộ nhỏ. Nên tiếp tục phát triển trên nền hiện tại, nhưng cần xử lý bảo mật template và tính bền vững của notification trước khi coi đây là kênh thông báo production đáng tin cậy.

Ưu tiên: **template security → transaction và event → worker claim/retry → quan sát và phục hồi → triển khai → tính năng mới**. Một replica chưa giải quyết gửi trùng vì background task của request và sweeper có thể chạy đồng thời.

Không nên đưa incident, DORA, AI và daily health reporting vào cùng đợt sửa nền tảng. `plan_v2/` đã mô tả một sản phẩm báo cáo có mục tiêu riêng; cần quyết định phạm vi sản phẩm trước khi mở rộng schema và scheduler.

## 2. Bằng chứng và giới hạn

- Đã đọc README, kiến trúc, roadmap, kế hoạch v2; tập trung source vào ingest, dispatch, auth/RBAC, template, retention, model và Docker.
- Chạy `pytest tests/ -q`: **260 passed, 86 skipped, 1 warning**. Chạy lại ngoài sandbox vẫn có 86 skipped. Không coi đây là kết quả kiểm thử tích hợp đầy đủ.
- Trong sandbox, test PostgreSQL bị skip vì kết nối localhost báo `Operation not permitted`. Chưa xác nhận nguyên nhân skip của lượt chạy ngoài sandbox.
- `ruff check app/ tests/`: **pass**.
- Kiểm chứng cục bộ template bằng phép cộng vô hại thông qua Python builtins: trả về `42`. Không đọc secret, không chạy shell, không sửa dữ liệu để kiểm chứng lỗi này.
- Chưa kiểm chứng deployment thật, restore backup, Teams tenant thật, load test hoặc HA. Các tình huống crash/concurrency dưới đây được suy ra từ source, chưa fault-inject trên PostgreSQL.
- Giữ nguyên các thay đổi có sẵn trong `docs/opstrail-overview.html`, file HTML cũ và `plan_v2/`.

## 3. Các phát hiện ưu tiên

### F1 — P0: Template preview vượt ranh giới quyền của viewer

Nguồn: `app/api/templates_admin.py:302–317`, `app/notifiers/render.py:31`, `app/notifiers/render.py:256`.

`POST /settings/template/preview` chỉ yêu cầu `current_user`, nhận `body` tùy ý rồi gọi `Environment.from_string(...).render(...)`. Environment là Jinja thường, không có sandbox. Kiểm tra JSON xảy ra sau render nên không ngăn việc thực thi biểu thức. Viewer có thể tiếp cận Python internals; điều này tạo khả năng thực thi mã trong quyền của process ứng dụng.

**Sửa:** trước mắt giới hạn preview theo quyền và vô hiệu hóa template tùy ý nếu chưa có biện pháp an toàn. Giải pháp bền vững: sandbox cập nhật, context là dữ liệu thuần thay ORM object, allowlist thuộc tính/filter/callable, hạn chế kích thước template và tài nguyên render. Nếu cho người dùng không tin cậy viết logic, cân nhắc render trong process cô lập. Chỉ chuyển endpoint sang admin chưa loại bỏ rủi ro từ template.

**Nghiệm thu:** viewer không được submit template; truy cập dunder/globals/builtins bị chặn; template chuẩn vẫn render; input lớn không treo API. Jinja lưu ý sandbox cần đi cùng giới hạn tài nguyên và dữ liệu truyền vào: [tài liệu sandbox](https://jinja.palletsprojects.com/en/stable/sandbox/).

### F2 — P1: Có thể lưu alert nhưng mất công việc gửi vĩnh viễn

Nguồn: `app/services/ingest.py:268`, `app/api/webhooks.py:63–66`, `app/services/dispatch.py:240`.

Ingest commit alert trước, queue notification commit sau. Nếu process chết hoặc queue lỗi ở giữa, alert đã tồn tại nhưng không có notification. Khi Alertmanager retry cùng trạng thái, ingest coi đó là repeat và trả `notify_ids=[]`; thông báo không được tạo lại.

**Sửa:** một transaction cho cập nhật trạng thái, tạo event và tạo delivery/outbox. Tầng service dùng flush, tầng điều phối commit một lần. Bổ sung unique key theo event/channel/đích và xử lý idempotency đồng thời; không chỉ unique theo alert/channel vì cùng alert có nhiều chuyển trạng thái.

**Nghiệm thu:** lỗi trước commit rollback cả alert/event/delivery; lỗi sau commit vẫn có delivery chờ; hai request trùng đồng thời tạo một event cho cùng chuyển trạng thái. Test replay cả firing và resolved.

### F3 — P1: Worker gửi trùng và commit theo batch quá rộng

Nguồn: `app/services/dispatch.py:244–274`, `app/api/webhooks.py:69`, `app/main.py:114`.

Worker SELECT pending không khóa/claim. Mỗi request còn khởi động một `dispatch_pending`, song song với sweeper. Hai lượt có thể gửi cùng row. Toàn bộ batch chỉ commit sau khi gửi xong; một exception hoặc process crash ở cuối có thể khiến các lần gửi thành công trước đó vẫn mang trạng thái pending trong DB.

**Sửa:** claim bằng transaction ngắn, dùng row locking và trạng thái processing có `lease_until`, `claim_token`; gửi ngoài transaction; lưu kết quả từng delivery và kiểm tra token khi hoàn tất. Giới hạn concurrency, timeout và lease; lease phải được gia hạn hoặc đủ dài cho công việc. Tách API và worker thành vai trò chạy của cùng codebase khi cần. Chưa cần thêm Kafka/RabbitMQ chỉ để xử lý vấn đề này.

PostgreSQL hỗ trợ `FOR UPDATE SKIP LOCKED` cho nhiều consumer trên bảng kiểu queue: [SELECT và locking](https://www.postgresql.org/docs/current/sql-select.htm). Dùng khóa để claim, không giữ transaction mở qua cả batch HTTP.

**Nghiệm thu:** hai worker không sở hữu cùng delivery còn lease; crash không làm mất việc; worker cũ không ghi đè kết quả worker mới. Chấp nhận rằng crash sau khi đích nhận HTTP nhưng trước commit vẫn có thể gây gửi lặp; cần idempotency phía nhận nếu muốn giảm khoảng mơ hồ đó.

### F4 — P1: Notification gửi trạng thái hiện tại thay vì trạng thái của event

Nguồn: `app/models/core.py:540–577`, `app/services/dispatch.py:171`, `app/notifiers/webhook.py:31–57`.

Notification giữ `alert_id` và target, nhưng khi gửi lại đọc Alert mutable. Nếu firing còn pending lúc resolved tới, cả notification firing và resolved có thể render thành resolved. Đổi metadata/template trước retry cũng có thể thay nội dung.

**Sửa:** event bất biến có status, timestamp, phiên bản và snapshot cần thiết; delivery trỏ vào event. Quyết định rõ gửi đủ chuỗi firing→resolved hay gộp sự kiện đã lỗi thời. Nếu cần retry đúng cùng thông điệp, lưu payload đã render và phiên bản template.

**Nghiệm thu:** chặn outbound, nhận firing rồi resolved, bật outbound; nội dung và thứ tự theo chính sách đã chọn, không có hai card resolved giả danh hai event.

### F5 — P1: Production có thể khởi động bằng secret công khai

Nguồn: `app/config.py:36–42`, `app/main.py:63–79`, `docker-compose.yml:67–73`.

Session secret mặc định và token dev đã công khai. Startup chỉ warning; intake token rỗng tắt xác thực. Cookie session chỉ chứa uid và dùng chữ ký, nên người biết session secret có thể giả chữ ký cho uid đang tồn tại.

**Sửa:** thêm chế độ môi trường rõ ràng; production từ chối secret rỗng/mặc định và cấu hình auth sai. Triển khai HTTPS, quản lý secret ngoài repo, rotation có runbook. Bổ sung CSRF cho form thay đổi dữ liệu, rate limit login/intake, giới hạn payload. SameSite=lax hiện có là lớp bảo vệ hữu ích nhưng chưa thay thế toàn bộ chính sách CSRF.

**Nghiệm thu:** production không boot bằng cấu hình dev; request sai token bị chặn; test CSRF và giới hạn login; reset mật khẩu/rotation có chính sách thu hồi session.

### F6 — P1: Retention có thể xóa alert đang firing

Nguồn: `app/services/retention.py:46–73`.

Purge theo `starts_at < cutoff` không xét status. Alert firing lâu hơn cửa sổ retention bị xóa cùng deliveries qua cascade. Repeat sau đó có thể tạo alert mới và gửi lại như sự cố mới.

**Sửa:** giữ alert còn hoạt động; retention lịch sử tính từ thời điểm resolved/closed theo chính sách. Cập nhật preview cùng predicate. Pending delivery chỉ được xóa theo chính sách kết thúc rõ ràng.

**Nghiệm thu:** alert firing cũ vẫn tồn tại qua purge; alert resolved đủ tuổi được xóa; repeat không phát sinh event mới do purge.

### F7 — P1: Chưa đủ khả năng phát hiện hỏng và khôi phục

Nguồn: `app/main.py:183–185`, `docker-compose.yml`, `app/scheduler.py`.

`healthz` cố định phù hợp liveness nhưng không chứng minh DB hoặc worker hoạt động. Chưa thấy endpoint metrics, worker heartbeat, quy trình backup/restore và cảnh báo backlog trong cấu hình được review. Scheduler in-memory được tạo lại khi restart; `misfire_grace_time` không tự tái dựng một kỳ đã lỡ lúc process tắt.

**Sửa:** giữ liveness đơn giản; thêm readiness kiểm tra DB có timeout; metrics/heartbeat cho worker và retention. Teams lỗi phải làm tăng backlog và cảnh báo, không nhất thiết làm API mất readiness. Lưu `last_success` cho job hoặc chạy kiểm tra overdue khi startup. Có backup ngoài máy và diễn tập restore.

**Nghiệm thu:** dừng worker nhưng API sống vẫn bị phát hiện; DB lỗi phản ánh readiness; restore bản backup sang môi trường sạch và đo RPO/RTO thực tế.

### F8 — P2: Deployment và CI chưa bảo đảm tái lập

Nguồn: `Dockerfile`, `pyproject.toml`, `entrypoint.sh:2–6`, `tests/test_ingest.py:44–51`.

Dependency chỉ có lower bound, chưa thấy lockfile hoặc pipeline CI trong checkout; container chưa có USER riêng. Compose được ghi rõ là dev stack, mở cả DB/mock và có credential mock. Mỗi replica tự migrate; không nên dựa vào comment rằng version table tự serialize toàn bộ migration. Fixture tạo schema bằng `create_all`, nên chưa chứng minh chuỗi Alembic chạy được; lỗi DB chuyển thành skip có thể làm CI xanh giả.

**Sửa:** lock dependencies, build/test cùng artifact, non-root image, cấu hình production tách dev. Migration một job trước rollout hoặc khóa migration rõ ràng. CI bắt buộc PostgreSQL disposable; kiểm thử Alembic từ DB rỗng và nâng cấp từ phiên bản trước; DB không khả dụng phải fail suite tích hợp. Fixture phải xác nhận DB test trước `drop_all`.

**Nghiệm thu:** build tái lập; full suite tích hợp không skip; migration và rollback ứng dụng được diễn tập; compose production không kéo mock hoặc mở DB công khai.

## 4. Kế hoạch thực hiện

Ước lượng sơ bộ cho một kỹ sư nắm stack; chưa phải cam kết, chưa gồm thời gian chờ hạ tầng hoặc review. F2/F3/F4 nên thiết kế chung để tránh migrate nhiều lần.

| Đợt | Công việc | Ước lượng | Điều kiện hoàn tất |
|---|---|---|---|
| A — Chặn rủi ro trực tiếp | F1, fail-fast secret của F5; tạo PostgreSQL test disposable | 2–4 ngày công | Template không vượt quyền; cấu hình prod an toàn; integration test chạy thật |
| B — Bảo đảm luồng thông báo | F2/F3/F4: event, outbox atomic, unique key, claim/lease, snapshot, retry | 5–8 ngày công | Test concurrent intake/worker, crash và replay đạt; có migration |
| C — Vận hành | F6/F7: retention an toàn, readiness, metrics, heartbeat, restore/runbook | 3–5 ngày công | Không xóa firing; phát hiện worker hỏng; restore đo được |
| D — Phát hành | F8, phần còn lại F5; CI, image, deployment, migration gate | 3–5 ngày công | Artifact kiểm chứng; rollback và pilot đạt |

Các đợt có giao nhau nên không cộng cơ học thành thời hạn lịch. Theo dõi pilot ít nhất 7 ngày sau đợt D, có tình huống Teams lỗi và restart có chủ đích trong môi trường thử nghiệm.

### Backlog nghiệm thu reliability

1. Gửi lại cùng payload tuần tự và đồng thời: một alert và một event cho cùng chuyển trạng thái.
2. Fault-inject giữa update alert và tạo delivery: rollback nguyên khối.
3. Restart sau commit trước outbound: worker tìm được delivery.
4. Hai worker tranh claim; worker chết; lease hết hạn; worker cũ hoàn tất muộn.
5. Firing→resolved trong lúc Teams ngắt: kiểm tra snapshot/thứ tự.
6. HTTP 429 có Retry-After, timeout, 5xx, lỗi 4xx vĩnh viễn: retry có phân loại, jitter và giới hạn theo đích.
7. Một delivery lỗi bất ngờ không rollback kết quả các delivery trước.
8. Retention giữ firing và các công việc còn xử lý theo chính sách.
9. Alembic từ DB trống và từ release trước; backup/restore; rollback image tương thích schema.

### SLI/SLO đề xuất cho pilot

Các con số sau cần được chủ hệ thống chấp thuận sau đo tải thật.

| Chỉ số | Mục tiêu khởi điểm / cách dùng |
|---|---|
| Intake availability | 99,9% trong 30 ngày; đo từ probe ngoài process và request thực |
| Intake latency | p95 dưới 1 giây ở tải đỉnh đã thống nhất |
| Delivery latency | p95 dưới 30 giây khi endpoint đích đáp ứng; theo dõi riêng độ trễ end-to-end gồm downtime |
| Pending oldest age | Cảnh báo khi quá 2 phút, nâng mức khi quá 10 phút; hiệu chỉnh theo tải |
| Worker heartbeat | Phát hiện thiếu heartbeat quá 60 giây |
| Backup | Đề xuất RPO 24 giờ, RTO 2 giờ; xác nhận bằng restore drill |

Ghi metrics theo channel/status, không gắn fingerprint, URL hay toàn bộ labels vào metric để tránh cardinality cao và lộ bí mật. Log có request_id/event_id/delivery_id, che URL query token và response nhạy cảm. Có cảnh báo qua kênh độc lập khi Teams hoặc chính OpsTrail hỏng.

## 5. Góp ý hướng sản phẩm và plan_v2

**Giữ:** mô hình service/team, phân quyền, ACK, label/severity rules, template fallback và lịch sử gửi. Đây là những phần có ích rõ ràng cho công cụ nội bộ. Giữ modular monolith; API và worker có thể dùng cùng image với command khác nhau.

**Chỉnh roadmap:** số liệu 131 test/3 migration trong `docs/roadmap.md` đã cũ; hiện có 8 file migration và 346 test được thu thập, trong đó 86 bị skip ở lượt kiểm tra này. Roadmap còn ghi mention là việc tương lai dù source đã có. Bảng notification hiện là bản ghi delivery tổng hợp với bộ đếm attempts, chưa phải lịch sử bất biến của từng lần thử như một số mô tả/comment có thể khiến người đọc hiểu.

**Daily health report:** kế hoạch v2 đi đúng hướng khi tách Healthy/Unknown, kiểm tra coverage, lưu kỳ báo cáo và xử lý SMTP ambiguous. Nên giữ như module/dự án riêng về phạm vi nghiệp vụ, có thể dùng chung package delivery/auth thay vì sao chép nguyên ứng dụng. Chưa tạo repo mới khi chưa chốt ownership và cách triển khai.

**Bổ sung vào P0 của v2:** hợp đồng query có đơn vị, loại metric, cửa sổ và step, độ trễ chấp nhận, expected targets, giới hạn series/response size và quyền đọc. Tách trạng thái tài nguyên, chất lượng dữ liệu và maintenance thành các chiều riêng. Khóa phiên bản query/config cho report; chốt late data/catch-up và quota nguồn. Báo cáo gửi theo team phải kiểm soát cả query scope lẫn recipient scope.

**Điều chỉnh thứ tự:** dựng một vertical slice trên một cluster, một nguồn, một check đã xác nhận → preview báo cáo → SMTP thử nghiệm → lịch bền vững và chạy bù → pilot. Sau đó mở rộng check và nguồn thứ hai. Cách này xác nhận được đường đi từ metrics tới người nhận sớm, đồng thời vẫn kiểm tra hai adapter trước khi tuyên bố hỗ trợ cả hai.

Incident/MTTR cần lifecycle riêng; thời gian alert firing không đại diện MTTR nghiệp vụ. Không dùng số lượng alert ít hoặc bằng không làm bằng chứng hệ thống khỏe. Partition chỉ quyết định sau khi đo query, vacuum, purge và kích thước bảng; mốc 100k alert/tháng trong docs chưa đủ để bắt buộc partition, nhất là unique key hiện tại cần được xem lại theo partition key.

## 6. Phạm vi đã xác nhận ngày 27/10/2026

| Nội dung | Quyết định của chủ dự án | Áp dụng vào kế hoạch |
|---|---|---|
| Người dùng | Sử dụng nội bộ trước mắt | Giữ RBAC; chưa đưa cách ly dữ liệu nhiều tenant vào đợt đầu. Nội bộ không đồng nghĩa bỏ bảo mật |
| Tải đỉnh | Dưới 100 message/giây | Lấy 100 alert event/giây làm giả định bảo thủ để benchmark; phân biệt với HTTP request/giây và card outbound |
| Sau outage | Tổng hợp lại các sự kiện cũ | Lưu đủ lịch sử event; gom backlog thành bản tổng hợp theo đích nhận, tránh phát lại hàng loạt card cũ |
| Triển khai | Docker Compose | Một codebase/image, vai trò API và worker riêng; job migration chạy một lần trước rollout |
| PostgreSQL | Team nội bộ sở hữu | Team quản lý DB; bàn giao backup/restore, quyền truy cập và quy trình migration |
| Mục tiêu | Củng cố OpsTrail để chạy trước | Hoãn daily health report và các tính năng mở rộng đến sau pilot ổn định |

Chưa có số message/ngày, thời lượng peak, số đích, thời gian outage cần giữ và cấu hình máy đích. Vì vậy chưa chốt CPU/RAM/dung lượng DB hoặc cam kết throughput. Quyền sở hữu backup và người nhận cảnh báo của chính OpsTrail cần được ghi tên cụ thể trong runbook; quyền sở hữu DB chưa tự xác nhận các trách nhiệm đó.

## 7. Thiết kế triển khai cho đợt củng cố

### Topology Docker Compose

```text
Alertmanager → reverse proxy/TLS → api → PostgreSQL ← worker → Teams/webhook
                                           ↑
                                migrate (one-shot, trước rollout)
```

- `api`: xác thực, UI và intake; commit alert/event/delivery nguyên khối; không chạy request background dispatcher.
- `worker`: claim delivery, gửi có giới hạn theo đích, retry và tổng hợp backlog; sở hữu retention/severity jobs với cơ chế chống chạy chồng. Khởi đầu một worker, vẫn kiểm thử hai worker để xác nhận claim đúng.
- `migrate`: dùng cùng image, credential phù hợp cho DDL; hoàn tất thành công mới chạy phiên bản ứng dụng mới. API/worker dùng quyền runtime hạn chế hơn.
- PostgreSQL có thể là instance do team vận hành ngoài Compose hoặc service trong Compose tùy hạ tầng hiện có; API dùng DSN được cấp. Nếu nằm trong Compose, không publish cổng DB ra mọi interface và không coi volume là backup.
- Reverse proxy có thể dùng hạ tầng TLS sẵn có của nội bộ. Mock Teams/Entra và Alertmanager demo thuộc dev profile.
- Đặt restart policy, giới hạn tài nguyên, log rotation, graceful shutdown và healthcheck theo vai trò. Một host Compose vẫn là một điểm lỗi; đây chưa phải thiết kế HA.

### Chính sách tổng hợp backlog sau outage

Đây là thiết kế đề xuất để thực hiện mong muốn tổng hợp sự kiện cũ; các giá trị thời gian cần cấu hình và hiệu chỉnh trong pilot.

1. Event firing/resolved vẫn được ghi bất biến, kể cả khi không gửi card riêng. Không xóa lịch sử để giảm backlog.
2. Worker theo dõi lỗi theo đích nhận, backoff có jitter và tôn trọng Retry-After. Việc một đích lỗi không chặn các đích còn lại. Recovery phải qua nhịp gửi có kiểm soát, không bung toàn bộ pending cùng lúc.
3. Khi phục hồi, claim một tập event cũ theo cutoff cố định, nhóm theo **đích nhận và phạm vi team**; trong bản tổng hợp nhóm tiếp theo service. Cấu hình ban đầu đề xuất: event chờ trên 5 phút đủ điều kiện tổng hợp. Đây chưa phải giá trị người dùng đã chốt.
4. Bản tổng hợp ghi rõ khoảng outage/backlog, số sự cố theo severity, số đã resolved và danh sách còn firing, kèm liên kết tới lịch sử. Đếm riêng alert episode và số event để không biến firing/resolved thành hai sự cố.
5. Lưu snapshot nội dung, danh sách event thành viên và delivery của bản tổng hợp trong DB. Event được gán vào bản tổng hợp một cách atomic, có ràng buộc chống hai worker gom cùng delivery nguồn. Chỉ đánh dấu đã được tổng hợp/gửi sau khi delivery tổng hợp thành công; retry dùng lại đúng snapshot.
6. Event mới sau cutoff đi theo luồng bình thường; không được đưa vào một bản tổng hợp đã đóng. Với alert đổi trạng thái trong lúc bản tổng hợp chờ gửi, hiển thị thời điểm snapshot rõ ràng và bảo đảm thứ tự theo đích hoặc quy tắc supersede đã kiểm thử để không đảo firing/resolved.
7. Giới hạn kích thước và số mục; báo cáo lớn được chia thành các phần có ID ổn định và liên kết xem đầy đủ trên UI. Tổng hợp theo từng đích, không gộp dữ liệu giữa các nơi nhận khác quyền.
8. Retry window hiện tại 60 phút không mặc nhiên là yêu cầu mới. Cần cấu hình riêng thời hạn giữ backlog có thể tổng hợp và chính sách hết hạn. Delivery đã failed vẫn phải có đường đưa vào tổng hợp theo chính sách, thay vì bị bỏ quên khi outage dài.

Nghiệm thu bổ sung: outage có firing→resolved→firing, restart trong lúc tạo digest, hai worker cùng gom, timeout sau khi đích có thể đã nhận, bản tổng hợp quá lớn, và một đích lỗi trong khi đích khác khỏe. HTTP thành công chỉ chứng minh endpoint chấp nhận request; việc card xuất hiện trong Teams cần kiểm tra end-to-end khi pilot.

### Benchmark tải đỉnh

- Đơn vị tạm dùng: **100 alert event/giây**; đo riêng HTTP request/giây với batch 1/10/100 alerts mỗi payload. Nếu “message” thực tế là webhook request thì phải tính lại tổng event từ kích thước batch.
- Bài test đề xuất: 10 phút ở 100 event/giây, gồm alert mới, repeat và chuyển trạng thái; thêm một đợt burst ngắn để đo giới hạn. Đây là workload kiểm thử, chưa phải giả định peak kéo dài cả ngày.
- Mock outbound có latency, 429 và lỗi có kiểm soát; không bắn 100 card/giây vào Teams thật. Tốc độ ingest và tốc độ đích nhận là hai giới hạn riêng.
- Đo p95/p99 intake latency, error rate, DB connection pool/CPU/IO, queue age, tăng trưởng storage, tốc độ gom và tiêu thoát backlog. Đối chiếu số event/delivery theo chính sách; không mất hoặc nhân đôi event do request concurrent.
- So sánh kịch bản outbound khỏe, một đích lỗi và tất cả đích lỗi. Ingest vẫn phải lưu bền vững khi outbound lỗi trong ngân sách tải đã thống nhất.
- Thời gian tiêu thoát backlog chỉ có thể chốt sau khi biết quota thực tế theo đích. Ước lượng dung lượng từ số event/ngày và bytes/event đo được, không nhân tải peak thành tải trung bình cả ngày.

## 8. Backlog thực thi cho bản chạy nội bộ

| Thứ tự | Hạng mục | Đầu ra kiểm chứng |
|---|---|---|
| 1 | Chặn template injection, production config validation; dựng DB test cô lập | Test quyền preview/sandbox; config yếu không boot; PostgreSQL integration suite chạy thật |
| 2 | Thiết kế schema event/outbox/digest và migration | Atomic ingest, unique event/delivery, snapshot; xử lý dữ liệu notification cũ có quy tắc rõ |
| 3 | Tách worker, claim/lease, per-delivery commit và rate limit theo đích | Test concurrency/crash/recovery, một đích lỗi không chặn đích khác |
| 4 | Tổng hợp backlog sau outage | Digest bền vững, retry đúng snapshot, có truy vết event nguồn và chống gom trùng |
| 5 | Retention an toàn, readiness/heartbeat/metrics, runbook | Giữ firing; phát hiện worker chết; backup/restore và cảnh báo có owner |
| 6 | Compose production, locked dependencies, migration gate và CI | Build cùng artifact; dev mocks tách riêng; rollback đã diễn tập |
| 7 | Benchmark và pilot | Đo ở 100 event/giây theo giả định nêu trên; 7 ngày pilot, có outage/restart thử nghiệm |

Bước 2–4 mở rộng khối lượng của đợt B so với ước lượng ban đầu vì bổ sung digest có trạng thái bền vững. Cần chia task và ước lượng lại sau thiết kế schema; không mặc định chức năng tổng hợp đã nằm trong 5–8 ngày công ban đầu.

**Điều kiện phát hành:** xử lý P0; atomic intake và recovery được kiểm chứng; full PostgreSQL suite không skip; có đo tải trên máy thử nghiệm tương đương máy đích; restore/rollback thực hiện được; backlog cũ được tổng hợp đúng phạm vi. Daily health report, incident/DORA và tính năng mở rộng chỉ bắt đầu sau khi bản này đạt pilot.
