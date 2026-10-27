# Hướng dẫn tạo template card MS Teams

Đi từ con số không tới một card dùng được. Trang này là **cách làm**; danh sách
biến và cơ chế chọn template nằm ở [teams-template.md](teams-template.md).

> Cần vai trò **editor** trở lên. Viewer xem được template nhưng không sửa.

---

## Trước khi bắt đầu: card nên nói gì

Người nhận card lúc 2 giờ sáng cần đúng bốn điều, theo thứ tự:

1. **Cái gì hỏng** — tên alert, mức độ
2. **Nặng đến đâu** — số đo hiện tại so với ngưỡng
3. **Ở đâu** — service, instance
4. **Làm gì tiếp** — runbook, đồ thị, link chi tiết

Mọi thứ khác là nhiễu. Team và owner **không cần** nằm trên card — chúng dùng
để định tuyến vào channel và dựng thống kê, là việc khác.

---

## Bước 1 — Tạo template

**Settings → Template Teams → gõ tên → Tạo.**

Template mới bắt đầu bằng **bản sao của built-in**, không phải trang trắng. Có
hai lý do: bạn có ngay một card chạy được để sửa dần, và một template rỗng thì
không render được nên lần lưu đầu tiên sẽ bị từ chối, khiến trình soạn trông
như đang hỏng.

---

## Bước 2 — Bộ khung tối thiểu

Template là **Jinja2 xuất thẳng ra JSON Adaptive Card**. Khung nhỏ nhất chạy
được:

```jinja
{
  "type": "AdaptiveCard",
  "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
  "version": "1.5",
  "msteams": { "width": "Full" },
  "body": [
    { "type": "TextBlock", "text": {{ alert.alertname | tojson }}, "wrap": true }
  ]
}
```

Ba thứ không được bỏ:

| | Vì sao |
|---|---|
| `"version": "1.5"` | Teams từ chối card thiếu version |
| `"wrap": true` | Thiếu nó, dòng dài bị cắt cụt thay vì xuống dòng |
| `\| tojson` | Xem bước 3 — đây là chỗ hỏng nhiều nhất |

---

## Bước 3 — Luật quan trọng nhất: `| tojson`

**Mọi giá trị chèn vào đều phải đi qua `| tojson`.**

```jinja
✗ "text": "{{ alert.summary }}"
✓ "text": {{ alert.summary | tojson }}
```

Chú ý cách viết đúng **không có dấu nháy bao ngoài** — `tojson` tự thêm.

Vì sao bắt buộc: summary của alert là văn bản tự do. Chỉ cần một dấu nháy kép
trong đó — `Disk "sda1" đầy` — là JSON vỡ, template hỏng, và OpsTrail phải lùi
về card mặc định. `tojson` escape hộ bạn dấu nháy, xuống dòng và unicode.

---

## Bước 4 — Luôn bọc trường có thể rỗng

Alert thật thường nghèo hơn bạn tưởng: không service, không runbook, không số
đo. Template cứng sẽ cho ra một cột dấu gạch ngang.

```jinja
{%- if value_line %},
{ "type": "TextBlock", "text": {{ value_line | tojson }}, "wrap": true }
{%- endif %}
```

Chú ý **dấu phẩy nằm sau `%}`**, không phải trước `{%`. Đặt sai chỗ là JSON
thừa hoặc thiếu dấu phẩy — lỗi hay gặp nhất sau `tojson`.

Những biến hay rỗng nhất: `value_line`, `duration_line`, `runbook_url`,
`dashboard_url`, `service`, `mention_text`.

> `severity` và `status` thì **không bao giờ rỗng** — thiếu severity, card tự
> hiện `INFO`. Không cần bọc hai cái này.

---

## Bước 5 — Xem trước

Preview tự dựng lại khi bạn gõ, sau khoảng nửa giây. Dùng cả **hai** chế độ:

| Chế độ | Để làm gì |
|---|---|
| **Alert thật gần nhất** | Thấy hình dạng dữ liệu của chính bạn — thường là thiếu `value`/`threshold` |
| **Alert mẫu đầy đủ** | Thấy card khi mọi trường đều có |

Template chạy với alert mẫu nhưng vỡ với alert thật là tình huống kinh điển, và
nó chỉ lộ ra lúc 3 giờ sáng nếu bạn không thử cả hai.

> Bản dựng trong trình duyệt là **xấp xỉ**, không phải ảnh chụp Teams. Mention
> hiện ra chữ `<at>Tên</at>` thô; Teams sẽ phân giải thành người thật. Cần đối
> chiếu từng pixel thì mở `JSON gửi đi` rồi dán vào
> [Adaptive Cards Designer](https://adaptivecards.io/designer).

---

## Bước 6 — Lưu

Bấm **Lưu**. OpsTrail render thử với **cả hai** ngữ cảnh ở bước 5 rồi mới ghi.
JSON sai thì **không lưu được** và template đang chạy giữ nguyên — bạn không
thể vô tình làm hỏng cảnh báo đang hoạt động.

---

## Bước 7 — Cho template chạy

Tạo xong chưa có nghĩa là được dùng. Hai cách:

- **Đặt mặc định** — mọi alert không khớp rule nào sẽ dùng nó.
- **Gắn rule** — ví dụ `severity=critical` + `tier=1` dùng card riêng, còn lại
  dùng mặc định. Xem [teams-template.md](teams-template.md#nhiều-template-chọn-theo-rule).

---

## Ví dụ hoàn chỉnh

Card gọn cho on-call. Copy vào trình soạn là chạy được.

```jinja
{
  "type": "AdaptiveCard",
  "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
  "version": "1.5",
  "msteams": { "width": "Full" },
  "body": [
    {
      "type": "TextBlock",
      "text": {{ (status_icon ~ " " ~ severity ~ " · " ~ (alert.alertname or "Alert")) | tojson }},
      "weight": "Bolder",
      "size": "Large",
      "color": {{ ("Good" if status == "RESOLVED" else "Attention") | tojson }},
      "wrap": true
    }
    {%- if mention_text and status != "RESOLVED" %},
    {
      "type": "TextBlock",
      "text": {{ mention_text | tojson }},
      "weight": "Bolder",
      "wrap": true,
      "spacing": "Small"
    }
    {%- endif %}
    {%- if value_line %},
    {
      "type": "TextBlock",
      "text": {{ value_line | tojson }},
      "size": "Large",
      "weight": "Bolder",
      "wrap": true
    }
    {%- endif %}
    {%- if alert.summary %},
    {
      "type": "TextBlock",
      "text": {{ alert.summary | tojson }},
      "wrap": true,
      "spacing": "Small"
    }
    {%- endif %}
    {%- if facts %},
    {
      "type": "FactSet",
      "spacing": "Medium",
      "facts": [
        {%- for title, value in facts %}
        { "title": {{ title | tojson }}, "value": {{ value | tojson }} }
        {{- "," if not loop.last }}
        {%- endfor %}
      ]
    }
    {%- endif %}
  ],
  "actions": [
    {%- set buttons = [] %}
    {%- if runbook_url %}{% set buttons = buttons + [("📖 Runbook", runbook_url)] %}{% endif %}
    {%- if generator_url and status != "RESOLVED" %}{% set buttons = buttons + [("🔍 Prometheus", generator_url)] %}{% endif %}
    {%- set buttons = buttons + [("🗂 OpsTrail", detail_url)] %}
    {%- for title, url in buttons %}
    { "type": "Action.OpenUrl", "title": {{ title | tojson }}, "url": {{ url | tojson }} }
    {{- "," if not loop.last }}
    {%- endfor %}
  ]
}
```

Mẫu này đã được render thử với alert đầy đủ (5 khối, 3 nút) và với alert nghèo
nhất có thể — không service, không số đo, không runbook, không cả alertname —
vẫn ra JSON hợp lệ (2 khối, 1 nút).

**Mẹo dựng danh sách nút:** gom vào một list Jinja rồi mới lặp, thay vì rải
`{% if %}` giữa các phần tử. Với cách rải, bạn phải tự tính xem dấu phẩy nào
còn cần — và nút cuối cùng bị tắt sẽ để lại một dấu phẩy thừa làm vỡ JSON.

---

## Lỗi hay gặp

| Triệu chứng | Nguyên nhân |
|---|---|
| `template did not produce valid JSON: Expecting ','` | Thiếu hoặc thừa dấu phẩy quanh một khối `{% if %}` |
| `Expecting value: line 1 column N` | Một biến rỗng chèn trần — thiếu `\| tojson` |
| Lưu được nhưng card thật là card mặc định | Template vỡ với **alert cụ thể đó**. Xem lý do ở cột *Chi tiết* trang Deliveries |
| Card hiện `<at>Tên</at>` thô | Mention đặt ngoài `TextBlock`/`FactSet`. Chỉ hai loại này hiển thị được |
| Card ra một cột dấu gạch ngang | Trường rỗng chưa bọc `{% if %}` |
| Sửa template mà card không đổi | Template chưa được đặt mặc định, hoặc một rule đang chọn template khác |
| Không có dòng số đo | Alerting rule chưa gửi annotation `value`/`threshold` — [alertmanager.md](alertmanager.md) |

---

## Trước khi đưa vào production

- [ ] Xem trước **cả** alert thật **lẫn** alert mẫu đầy đủ
- [ ] Thử với một alert **đã resolved** — `duration_line` có, `value_line` thường không
- [ ] Mọi trường tuỳ chọn đều bọc `{% if %}`
- [ ] Mọi giá trị đều qua `| tojson`
- [ ] Đã đặt mặc định hoặc đã gắn rule
- [ ] Bắn thử một alert thật và nhìn card trong Teams

Nếu template vỡ sau khi lên production, **alert vẫn tới**: OpsTrail tự lùi về
card mặc định và ghi lý do vào trang Deliveries. Bạn không mất cảnh báo, chỉ
mất định dạng đẹp.

---

[← Về README](../README.md) · [Tham chiếu template](teams-template.md) ·
[Tích hợp Alertmanager](alertmanager.md)
