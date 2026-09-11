File `monitoring/prometheus/prometheus.yml` là config của **Prometheus** — thành phần **nhận/gom dữ liệu metrics** từ nhiều nguồn (cAdvisor, node-exporter, bản thân Prometheus), lưu vào time-series DB, và là nguồn để `billing-sync` + Grafana đọc.

## Ý nghĩa từng phần — theo luồng dữ liệu (use case 3 & 4)

### Global settings

- `scrape_interval: 5s` — mặc định Prometheus scrape (lấy metrics) mỗi 5 giây cho mọi scrape job (trừ khi job override).
- `evaluation_interval: 15s` — tần số đánh giá rules (alerting/recording) — hiện không có rule nào, nên giá trị này thực tế không ảnh hưởng gì, chỉ mang tính chuẩn cấu hình.

### Scrape configs — 3 nguồn chính

#### 1. `job: prometheus` (self-monitoring)
- Target `prometheus:9090/metrics` — Prometheus expose metrics của chính nó (scrape duration, storage, rules, ...) ra cho monitoring.
- Nhãn thêm `job: prometheus` để phân biệt series tự đo.

#### 2. `job: cadvisor` (per-container metrics — **quan trọng nhất cho usage**)
- Target `cadvisor:8080` — cAdvisor sinh metrics của từng container: CPU, RAM, network, disk, v.v.
- Nhãn `job: cadvisor` đi kèm mọi series.
- Series chứa labels từ container thông qua cAdvisor, là nguồn của:
  - `container_cpu_usage_seconds_total{name="...", container_label_coder_owner="mike", ...}`
  - `container_memory_usage_bytes{...}`
  - `container_network_receive_bytes_total{interface="eth0", ...}`
  - `container_network_transmit_bytes_total{...}`
  - `container_fs_write_bytes_total{...}`
- Mình đã verify trong hệ thống này: instant query `container_cpu_usage_seconds_total{container_label_coder_owner!=""}` trả series của `mike` và `anna`, là data dùng cho **Grafana dashboard** và **billing-sync delta computation**.
- Nhãn `container_label_coder_owner` / `container_label_coder_workspace_id` được cAdvisor đẩy từ labels `coder.owner` / `coder.workspace_id` trên container (do Coder `docker run` gán khi tạo workspace — thiết kế trong template `main.tf`).

#### 3. `job: node-exporter` (host metrics)
- Target `node-exporter:9100` — đo host (CPU, memory, disk, filesystem, network interface level, v.v.).
- Nhãn `job: node` — series như `node_cpu_seconds_total`, `node_memory_MemAvailable_bytes`, `node_network_*`.
- Dashboard `gpu-usage.json` cũng dùng một số node metrics (ví dụ CPU host tổng) — nhưng để usage per-user thì cadvisor là nguồn chính.

#### 4. GPU exporter (commented out)
- Comment mẫu cho NVIDIA GPU exporter lẻ tùy chọn (`host.docker.internal:9091` hoặc bên ngoài), chờ user bật nơi host Linux có GPU. Hiện tại không active — không ảnh hưởng gì.

### Vai trò trong 2 use case chính

#### UC3 — Hiển thị usage trên Grafana
- Grafana dashboard query Prometheus:
  - Series `container_cpu_usage_seconds_total` → `rate(...[5m])` → sum by `container_label_coder_owner`.
  - Series `container_memory_usage_bytes` → current value / 1024^3 → GB.
  - Series `container_network_*_bytes_total` → `rate(...[5m])` → network in/out per user.
  - Series `container_fs_write_bytes_total` → `rate(...[5m])` → disk writes per user.
- Nhờ nhãn `container_label_coder_owner`, query sum by owner phân tách được metrics của `mike` / `anna`.
- Logs side: Prometheus không quản lý log — log là Loki/Promtail. Tuy nhiên dashboard còn có Logs panel query Loki, không phải Prometheus.

#### UC4 — Billing → Lago (chuỗi cổng-started → collect → lưu trữ → hiển thị tiền)
- `billing-sync` (`sync.py`) query Prometheus **instant query** (dùng `/api/v1/query` với `container_cpu_usage_seconds_total{container_label_coder_owner!=""}` và metric tương tự) — không dùng range query.
- Với mỗi metric trong `USAGE` dict (`cpu_seconds`, `ram_gb_hours`, `network_gb`, `disk_write_gb`), sync:
  - Lấy current value của series có tên container (`name`) — tên được suy ra từ `coder-<owner>-<ws>`.
  - So sánh với state trước đó trong `data/sync/state.db` (`counters` table, key `name:code`) → tính delta.
  - Nếu delta > 0 → gom theo owner, accumulation.
- Sau mỗi chu kỳ (~60s), sync POST `/api/v1/events` đến Lago API với:
  - `event_name: "gpu.usage"` (hoặc tên event tương ứng)
  - `data: {customer_id: ..., properties: [{name: "cpu_seconds", value: ...}, ...]}`
- Lago nhận event → aggregate theo billable metric `sum` → cộng vào subscription của customer → tiền hiện trong Lago UI (usage meter + invoices).
- State db (`state.db`) đảm bảo restart/sync crash không mất baseline → không double-count.

### Lưu ý thêm
- Series LAG: vì chỉ scrape 5s, delta computation của sync tính trên sự khác biệt giữa 2 lần query liên tiếp — accuracy ở mức xấp xỉ 5-60s, đủ cho demo usage-based billing.
- [Bỏ comment GPU unpause] khi đã có GPU exporter host.
- `scrape_interval` job có thể override riêng (ở đây đều 5s, consistent).

Tóm lại, file này nói cho Prometheus **ai cung cấp metrics**, **bao lâu scrape một lần**, và **nhãn nào đính kèm** — để từ đó Grafana + billing-sync biết query series nào, filter theo owner nào.

Nếu bạn muốn thêm recording rules (tính sẵn rate/aggregate rồi lưu series trung gian) hoặc alert rules (ví dụ alert nếu container không xuất hiện sau 2 phút) mình có thể bổ sung.

# Giải thích thêm về cAdvisor: Prometheus chủ động pull data từ cAdvisor đúng không?
 Đúng. Với lưu ý quan trọng: **"call" ở đây là pull (Prometheus chủ động scrape HTTP endpoint của cAdvisor), không phải cAdvisor gọi Prometheus report data**. Cụ thể:

## Luồng đúng

1. **cAdvisor chạy độc lập** như một service trong compose (`cadvisor` service), lắng nghe `cadvisor:8080` (và port host `9091`). Nó quét Docker socket (`/var/run/docker.sock`), đo metrics của mọi container, expose HTTP endpoint `/stats`, `/metrics`, `/docker/<id>`,...
2. **Prometheus là scrape pull**: `prometheus.yml` khai báo `scrape_configs` job `cadvisor` target `cadvisor:8080` với `scrape_interval: 5s`. Mỗi 5 giây Prometheus tự mở HTTP request đến cAdvisor để lấy metrics → lưu vào time-series DB của mình.
3. **Grafana + billing-sync không "call" cAdvisor trực tiếp** — chúng gọi Prometheus (Grafana query Prometheus DataSource; billing-sync query Prometheus `/api/v1/query`) → trả về series đã lưu.

## Tại sao thiết kế pull

- Prometheus là **pull model**: nó là người chủ động lấy data — không cần cAdvisor push. Ưu điểm: Prometheus контроль tần suất scrape, retry, recording, dễ scale, không mất data khiมี burst.
- cAdvisor chỉ cần expose metrics đúng format (Prometheus exposition format) — không phụ thuộc Prometheus.

## Compose binding

- cAdvisor mount `/var/run/docker.sock` để đọc container metadata + stats. Không cần Prometheus mount socket — Prometheus chỉ HTTP client đến cAdvisor.
- Nhãn `coder.owner` / `coder.workspace_id` mà bạn thấy trong metric là do **cAdvisor đọc labels của container từ Docker** (Coder `docker run` tạo container kèm labels tersebut), không phải Prometheus tạo — Prometheus chỉ nhận series đã có sẵn labels.

## Cách kiểm tra nhanh

1. Shell vào Prometheus: `docker exec prometheus curl -s http://cadvisor:8080/metrics | head` → xem cAdvisor expose metrics trực tiếp.
2. Query Grafana: `container_cpu_usage_seconds_total{container_label_coder_owner!=""}` → dữ liệu đến từ Prometheus (đã scrape từ cAdvisor).

Tóm: **cAdvisor chạy độc lập + Prometheus "gọi" (scrape pull) data từ cAdvisor, đúng như bạn hiểu — chỉ là "gọi" theo cơ chế HTTP scrape định kỳ, không phải cAdvisor push.**