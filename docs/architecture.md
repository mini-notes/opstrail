# Kiến trúc

Những hành vi của hệ thống mà bạn cần biết trước khi vận hành hoặc sửa nó.

## Hành vi cần biết

**Key `(fingerprint, starts_at)` + UPSERT.** Alertmanager gửi lại mỗi
`repeat_interval`. INSERT thuần sẽ biến 1 alert thành hàng chục row.

**Chỉ notify khi alert MỚI hoặc ĐỔI TRẠNG THÁI.** Repeat vẫn cập nhật row
nhưng không gửi lại card. Nếu không, `repeat_interval: 1m` sẽ dội Teams mỗi
phút cho cùng một sự cố.

```
lần 1  {"received":1,"notified":1,"queued":2}
lần 2  {"received":1,"notified":0,"queued":0}   <- repeat, im lặng
resolved {"received":1,"notified":1,"queued":2} <- đổi trạng thái, có gửi
```

**Một transaction cho alert và công việc gửi.** Intake cập nhật alert và tạo
notification snapshot trong cùng transaction rồi mới trả 202. Teams chết thì
cả lịch sử và công việc gửi vẫn còn. Lỗi trước commit → rollback và trả 5xx để
Alertmanager retry. Worker gửi sau commit, dùng snapshot thay vì trạng thái
Alert mutable.

**Retry và backlog.** Worker dùng lease theo đích, backoff mũ có jitter và
Retry-After. Cửa sổ backlog mặc định `BACKLOG_RETENTION_HOURS=168`; các setting
DISPATCH_GIVE_UP_AFTER_MINUTES/MAX_ATTEMPTS cũ không còn điều khiển worker.
Sự kiện chờ quá 5 phút được tổng hợp theo đích/team, nội dung và membership cố
định qua retry. Xem [vận hành](operations.md) để biết quota, requeue và giới
hạn đảm bảo khi crash sau khi HTTP đã được nhận.

**`endsAt = 0001-01-01T00:00:00Z`** (Go zero time) được chuyển thành `NULL`.
Lưu nguyên sẽ khiến mọi alert trông như đã resolved từ năm 1.

---

## Dashboard

OpsTrail có sẵn `/dashboard`. Nếu muốn phân tích sâu hơn thì trỏ Grafana vào
Postgres này — không cần viết thêm code. Có sẵn:
`idx_alerts_starts_at_desc`, `ix_alerts_service_starts_at`, GIN trên `labels`.

```sql
-- alert theo service, 30 ngày
SELECT s.key AS service, count(*) FROM alerts a
JOIN services s ON s.id = a.service_id
WHERE a.starts_at > now() - interval '30 days'
GROUP BY 1 ORDER BY 2 DESC;

-- alert ồn nhất
SELECT alertname, count(*) AS n,
       avg(ends_at - starts_at) FILTER (WHERE ends_at IS NOT NULL) AS avg_duration
FROM alerts WHERE starts_at > now() - interval '30 days'
GROUP BY 1 ORDER BY 2 DESC LIMIT 20;

-- notification hỏng
SELECT channel, status, count(*) FROM notifications
WHERE created_at > now() - interval '7 days' GROUP BY 1,2;
```

> **MTTR cho DORA phải tính trên `incidents`, không phải `alerts`.** Một alert
> flapping sinh hàng chục cặp firing/resolved cho cùng một sự cố. Bảng
> `incidents` đã tạo sẵn ở Phase 1 để Phase 2 không phải migrate.

---


---

[← Về README](../README.md)