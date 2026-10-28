# Đề xuất Monitoring Topology & Coverage cho OpsTrail

Ngày nghiên cứu: 28/10/2026. Trạng thái: phân tích thiết kế, chưa triển khai. Đã đọc cấu trúc OpsTrail hiện tại; chưa đọc repo GitOps của người dùng vì chưa có URL/quyền truy cập trong phiên này.

## 1. Kết luận và mục tiêu

Khả thi nếu định nghĩa sản phẩm là bản đồ hệ thống và mức phủ monitoring có bằng chứng. Giá trị chính: biết đối tượng nào cần được monitor, đã khai báo gì, thực tế được thu thập gì, rule nào kiểm tra điều kiện nào, phần nào chưa được phủ và ai chịu trách nhiệm.

Chia ba nguồn sự thật:

- **Catalog kỳ vọng:** hệ thống/tài nguyên phải tồn tại và yêu cầu monitoring.
- **GitOps:** ý định cấu hình theo môi trường, commit, rendered artifact và deployment mapping.
- **Runtime:** bằng chứng hiện thời từ collector/rule evaluator/metrics backend.

Discovery từ monitoring config chỉ thấy những gì đã được khai báo. Catalog riêng giúp hiện cả hệ thống chưa có monitoring. Đồ thị thu thập cũng không tự chứng minh quan hệ gọi nhau của ứng dụng.

Phạm vi đầu: catalog + coverage matrix + graph liên kết cấu hình/rule. Dependency map nghiệp vụ chỉ dùng quan hệ khai báo được duyệt; bằng chứng traffic/tracing là một lớp mở rộng.

## 2. Tham khảo và điều rút ra

- [Prometheus configuration](https://prometheus.io/docs/prometheus/latest/configuration/configuration/): target có thể tĩnh/động; target relabel và metric relabel tác động ở các bước khác nhau. Vì thế đọc YAML gốc chưa đủ mô tả trạng thái cuối.
- [Prometheus Operator](https://prometheus-operator.dev/docs/api-reference/api/): nếu dùng Kubernetes, khảo sát ServiceMonitor, PodMonitor, Probe, ScrapeConfig và PrometheusRule. [Troubleshooting](https://prometheus-operator.dev/docs/platform/troubleshooting/) hữu ích cho việc tài nguyên đã khai báo nhưng selector/port không tạo target mong muốn.
- [Backstage System Model](https://backstage.io/docs/features/software-catalog/system-model/): tham khảo ranh giới System, Component, Resource và quan hệ ownership. Có thể dùng schema tương thích một phần, không cần triển khai cả Backstage chỉ để có catalog.
- [Grafana Tempo service graph](https://grafana.com/docs/tempo/latest/metrics-from-traces/service_graphs/service-graph-view/): bản đồ dịch vụ dựa trên tracing là hướng bổ sung khi muốn quan sát phụ thuộc runtime.
- [Multi-target exporter](https://prometheus.io/docs/guides/multi-target-exporter/): exporter/prober và đối tượng được đo có thể khác nhau; đặc biệt quan trọng với blackbox/SNMP.

Đề xuất của tài liệu này là thiết kế riêng cho nhu cầu OpsTrail, không phải chức năng sẵn có đã được xác nhận của các sản phẩm trên.

## 3. Các câu hỏi UI cần trả lời

1. Có bao nhiêu system/service/resource thuộc từng team/environment? Đối tượng nào chưa có monitoring declaration?
2. Mỗi đối tượng được kiểm tra availability, performance, capacity, saturation, correctness, backup/freshness hay certificate theo profile nào?
3. Thu thập bằng application instrumentation, exporter, probe hay nguồn tổng hợp? Ai vận hành collector và exporter?
4. Git khai báo gì, artifact nào được render, collector nào đã nạp? Có lệch với runtime không?
5. Có dữ liệu mới không? Metric nào được dùng cho rule, metric nào chỉ thu thập, rule nào thiếu input/đang evaluation error?
6. Thiếu ownership, runbook, routing hoặc yêu cầu monitoring nào? Ai xử lý và file/commit nằm ở đâu?

Không gom tất cả thành một dấu xanh. Một rule có thể đang evaluate thành công nhưng điều kiện cảnh báo đang firing; collector có thể hoạt động nhưng coverage vẫn thiếu.

## 4. Mô hình đối tượng và quan hệ

### Đối tượng

| Đối tượng | Ý nghĩa |
|---|---|
| System | Hệ thống nghiệp vụ hoặc platform có ranh giới đã thống nhất |
| Service | Đơn vị logic có identity ổn định và owner |
| Component | Vai trò/khối có nhu cầu quan sát riêng, ví dụ api, worker, database |
| Resource | Node, cluster, database dùng chung, queue, external endpoint… |
| Deployment | Hiện thân theo environment/cluster/namespace/site; không đồng nhất với service logic |
| Collector / Target / Exporter | Thành phần thu thập, đối tượng scrape và exporter/prober, có identity riêng |
| Metric family / Rule | Tên metric, metadata; recording rule hoặc alerting rule |
| Monitoring profile | Yêu cầu monitoring theo loại đối tượng và mức quan trọng |

MVP có thể gộp Component/Resource vào một bảng entity có kind. Không cần bắt buộc mô hình bảy cấp: một service đơn giản không nhất thiết có module riêng. Module code chỉ xuất hiện khi catalog/instrumentation có định danh, không suy ra từ tên alert.

### Quan hệ có kiểu

- `part_of`, `owned_by`, `deployed_as`.
- `scrapes`, `probes`, `exposes_metric`, `uses_metric`, `derived_from`.
- `checks`, `notifies`, `depends_on`.

`depends_on` cần nguồn khai báo hoặc bằng chứng runtime riêng. Hai service cùng xuất hiện trong một query/rule không đủ chứng minh chúng gọi nhau.

Mỗi node/edge giữ source, commit/ref, file và vị trí YAML hoặc source map, thời điểm quan sát, phạm vi deployment, confidence và giải thích. Các cạnh suy luận phải phân biệt với cạnh khai báo. Regex không xác định được quan hệ thì để unresolved, không nối bừa.

Định danh: service key là logic ổn định; deployment key bổ sung env và các scope cần thiết. Job/instance/IP/pod không tự trở thành service ID. Hai replica Prometheus quan sát một target không được biến thành hai service; giữ riêng quan hệ collector và bằng chứng từng nguồn.

## 5. Chuẩn hóa metadata

Dùng `team` số ít để khớp OpsTrail hiện tại. Chốt ý nghĩa nhãn trước khi chốt tên.

| Trường | Quy ước đề xuất |
|---|---|
| `system`, `service` | ID trong catalog, không dùng tên hiển thị làm identity |
| `team` | Owner vận hành của đối tượng; mapping tới Team.key |
| `environment` | Bộ giá trị kiểm soát: prod/staging/dev… |
| `component` | api/worker/database/cache/queue… |
| `cluster`, `namespace`, `region` | Scope triển khai khi phù hợp, không bắt buộc với mọi third-party endpoint |
| `layer` | application/infrastructure/data/platform… |
| `management` | internal/external; phân biệt với công nghệ và layer |
| `monitoring_profile` | Chính sách yêu cầu; ưu tiên lưu trong catalog, không nhất thiết thành label trên mọi series |
| `rule_id`, `rule_owner` | Định danh rule ổn định và người sở hữu rule; tách khỏi team của tài nguyên được kiểm tra |
| `signal` | availability/latency/errors/capacity/backup/certificate… |

`infra`, `app`, `third_party` không hoàn toàn loại trừ nhau. Managed PostgreSQL có thể thuộc data layer, do bên ngoài quản lý và được nhiều ứng dụng sử dụng. Owner tài nguyên cũng khác owner từng ứng dụng dùng nó.

Không tự ghi đè label `service` nếu metric hiện dùng nó để chỉ Kubernetes Service hoặc nghĩa khác. Khi có xung đột, dùng nhãn riêng như `catalog_service` ở nguồn và mapping rõ về canonical service trong OpsTrail. Tránh nhân bản mọi nhãn nghiệp vụ lên mọi metric trước khi đo cardinality và kiểm tra label collision.

### Ví dụ catalog, không phải schema Prometheus/Backstage có sẵn

```yaml
apiVersion: opstrail.io/v1alpha1
kind: MonitoredEntity
metadata:
  key: payments-api
spec:
  system: payments
  kind: service
  owner: payments
  layer: application
  management: internal
  deployments:
    - key: payments-api-prod-hcm
      environment: prod
      cluster: prod-hcm
      namespace: payments
      monitoringProfile: http-api-critical-v1
  dependsOn:
    - resource: payments-postgres
    - resource: payment-provider
```

Catalog có thể nằm trong repo GitOps hoặc được tham chiếu từ catalog chuẩn của tổ chức. Không duy trì hai nơi cùng là nguồn authoritative cho ownership; OpsTrail lưu mirror, provenance và override tạm có audit khi thật sự cần.

## 6. Phát hiện và đối chiếu

### Git ingestion

Đọc commit đã xác định, parse YAML an toàn theo allowlist path/format, giới hạn file/depth/alias. Khảo sát format trước: Prometheus thuần, file_sd, Helm values, Kustomize, Jsonnet hoặc Operator CRD. Parser báo rõ format chưa hỗ trợ.

Với template/overlay, ưu tiên rendered artifact có source map và commit từ pipeline được tin cậy. Không chạy shell/plugin/script từ repo trong process API hoặc chỉ vì có quyền đọc. Lưu phiên bản chart/dependency/render tool để có thể tái lập.

Một branch HEAD không mặc nhiên là revision đang triển khai. Map environment tới deployment revision/artifact. Không khẳng định runtime nạp đúng SHA nếu chưa có bằng chứng pipeline/release; hash cấu hình chuẩn hóa chỉ là một cách đối chiếu bổ sung.

### Runtime enrichment

Theo [Prometheus HTTP API](https://prometheus.io/docs/prometheus/latest/querying/api/), các endpoint cần khảo sát gồm `/targets`, `/rules`, `/status/config`, `/metadata`, `/targets/metadata`, query và series. Dùng đúng khả năng/version nguồn: metadata không tự chứng minh sample mới; metadata toàn cục không đủ gắn metric vào target.

Thiết kế adapter/capability detection cho collector, rule evaluator và storage. Nếu dùng vmagent/vmalert/VictoriaMetrics, cần đọc đúng thành phần thực thi thay vì mặc định một query endpoint biết toàn bộ scrape/rule. Kiểm chứng API cụ thể ở bước khảo sát, không cam kết tương thích chỉ từ tên backend.

Không quét toàn bộ series định kỳ. Cache metric family, giới hạn nguồn/selector/time window/concurrency/response size; inventory rộng chạy chậm hơn target health. Theo dõi tải do discovery tạo ra. Không trực tiếp probe mọi URL đọc được từ repo; endpoint runtime phải nằm trong danh sách được phép.

### Rule mapping

Parse PromQL thành AST; tham khảo [parser chính thức](https://github.com/prometheus/prometheus/blob/main/promql/parser/parse.go). Đi theo recording-rule dependency rồi tới metric selector, scope và label transformations. Bộ parser phải phù hợp dialect/version nguồn; biểu thức không hỗ trợ được giữ nguyên và đánh dấu unresolved.

Ví dụ `sum(rate(http_requests_total{job=~"api-.*"}[5m]))` không cho kết luận rule thuộc một service cụ thể. Aggregation có thể bỏ service/team; rule labels cũng có thể ghi đè attribution. Tài liệu [PromQL operators](https://prometheus.io/docs/prometheus/latest/querying/operators/) là cơ sở kiểm tra semantics. Rule dùng chung nên có binding/selector rõ, không copy cùng rule cho mọi service chỉ để điền nhãn.

Bắt buộc lưu quan hệ nhiều-nhiều. Một rule có thể áp cho hàng trăm resource; một service có nhiều collector/probe/rule. Phân biệt metric được tham chiếu trong rule, được exporter khai báo và được quan sát có sample mới.

## 7. Coverage và trạng thái

Mỗi deployment có các chiều trạng thái độc lập:

| Chiều | Ví dụ trạng thái |
|---|---|
| Inventory | expected / discovered-only / missing metadata |
| Declaration | defined / missing / unsupported / parse error |
| Deployment | loaded / mismatch / not verified |
| Collection | up / down / target missing / unknown / stale evidence |
| Metric evidence | observed-recent / referenced-only / not observed / not applicable |
| Rule | loaded / evaluation error / missing input / binding unresolved |
| Coverage | satisfied / partial / missing / exempt / not applicable |

Policy profile định nghĩa requirement có ID, scope, evidence freshness, signal, metric/query contract và rule binding cần thiết. Ví dụ HTTP API cần availability, error rate, latency và saturation phù hợp; PostgreSQL cần kết nối/dung lượng/replication/backup khi có quyền quan sát; third-party API cần probe/latency/cert hoặc quota mà mình thực sự thu thập được.

Coverage đếm requirement thỏa điều kiện trên các deployment kỳ vọng, loại trừ N/A/ngoại lệ đã duyệt khỏi denominator theo chính sách công bố. Giữ riêng phần Unknown; không nâng thành pass vì không có dữ liệu. Ngoại lệ có owner, lý do và ngày hết hạn.

Có rule latency chỉ chứng minh đã có kiểm tra latency; cần xem scope và bằng chứng dữ liệu. Nhiều rule không tự làm coverage tốt hơn. Source timeout không có nghĩa tất cả node đã bị xóa: giữ last-good snapshot và đánh dấu stale; chỉ kết luận removed sau một lần sync đầy đủ thành công với chính sách grace rõ ràng.

## 8. Trải nghiệm sử dụng

Ưu tiên matrix làm trang chính, graph dùng để drill-down:

- Cây Team → System → Service/Resource → Deployment.
- Matrix hàng là deployment, cột là các signal theo profile; ô mở ra bằng chứng metric/rule/Git/runtime và nguyên nhân thiếu.
- Graph theo từng system; tách lớp tổ chức, monitoring pipeline và dependency nghiệp vụ. Mặc định gộp replica; không vẽ từng time series.
- Entity detail: owner, profile, collection, metric families, rules, alert hiện tại, runbook và liên kết Git tại SHA cụ thể.
- Gap list: chưa khai báo, thiếu owner, selector không tạo target, có metric nhưng thiếu rule, rule thiếu input, Git/runtime drift.
- Change view giữa hai commit/snapshot: thêm/xóa target, đổi ownership, mất một nhóm rule hoặc coverage giảm.

Ví dụ ô latency: “Rule có trong Git; chưa xác nhận được evaluator nạp; metric chưa có evidence mới” thay vì dấu xanh “đã monitor”. Dữ liệu mô phỏng trên mockup phải được gắn nhãn.

## 9. Tích hợp vào OpsTrail

Tận dụng Team/Service, RBAC, audit, UI và PostgreSQL hiện có. Tách worker discovery khỏi intake/dispatch và đặt ngân sách DB/CPU/query riêng để scanner lỗi không ảnh hưởng nhận alert. Discovery sync có claim/lease, snapshot và last-good state riêng.

Các nhóm bảng đề xuất: repository_sources, source_snapshots, catalog_entities, entity_deployments, monitor_bindings, metric_families, monitoring_rules, graph_edges, coverage_requirements và coverage_findings. Thiết kế cụ thể sau khảo sát; Postgres với JSONB/edge table đủ cho pilot, chưa có bằng chứng cần graph database.

Link Alert tới deployment bằng identity/mapping có scope. Trước mắt là overlay alert hiện tại lên topology, không đổi routing production tự động theo suy luận discovery. Gán nhãn OpsTrail thủ công không sửa config gốc trong Git; hiển thị cả hai lớp để dễ phát hiện lệch.

## 10. Lộ trình và nghiệm thu

| Bước | Phạm vi | Nghiệm thu |
|---|---|---|
| P0 — Khảo sát | Một repo, một environment và vài đối tượng đại diện; chọn conventions | Mapping file → deployment → entity → owner được kiểm tra tay; xác định format/phạm vi chưa hỗ trợ |
| P1 — Git inventory | Catalog, parser, provenance, matrix configured-only, graph nhỏ | Có hệ thống expected chưa monitor; mọi khai báo có link nguồn; unsupported không bị mất im lặng |
| P2 — Runtime reconcile | Một collector/evaluator, target/rule/metric evidence và drift | Phân biệt declared/loaded/observed; mô phỏng selector sai, collector lỗi, metric stale, Git chưa deploy |
| P3 — Coverage policy | 2–3 profile, CI validation, diff và ngoại lệ | Requirement có bằng chứng; rule chung gắn đúng scope; source outage không xóa topology |
| P4 — Mở rộng | Nhiều environment/source, alert overlay và báo cáo | HA không nhân đôi entity; query discovery trong ngân sách; review chất lượng mapping |

Mẫu pilot nên gồm: một service API, một DB dùng chung, một third-party probe, một rule dùng recording rule, một target dynamic và một hệ thống cố tình chưa monitor. Chưa ước lượng ngày công trước khi biết tỷ trọng Helm/Jsonnet/dynamic discovery và mức chuẩn hóa repo.

CI GitOps có thể bổ sung schema/lint, labels/owner, rule ID, source-map consistency, diff coverage và kiểm thử rule. Dùng [promtool rule tests](https://prometheus.io/docs/prometheus/latest/configuration/unit_testing_rules/) cho tình huống có/mất dữ liệu; syntax pass chưa đủ chứng minh runtime và routing. Giai đoạn đầu chỉ báo finding và đề xuất thay đổi để người quản trị duyệt.

## 11. Quyền đọc và thông tin cần khảo sát

[GitLab Repository Files API](https://docs.gitlab.com/api/repository_files/) mô tả quyền đọc bằng read_repository/read_api tùy cách truy cập. Chọn scope nhỏ nhất cho endpoint thật sự dùng; đọc pipeline artifacts có thể cần thêm quyền API. Token theo project khi phiên bản/license hỗ trợ, có expiry/rotation; không cần write_repository hoặc quyền ghi cluster.

Cần URL GitLab/repo, branch và mapping môi trường, thư mục scrape/rules/catalog, cách render/deploy, danh sách collector/evaluator/backend và một vài file đại diện. Sau đó mới xác định API/version và quyền runtime đọc tối thiểu. Không yêu cầu gửi token trong chat; cấu hình qua secret/connector được cấp.
