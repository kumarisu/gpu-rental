File `monitoring/promtail/promtail.yml` là config của **Promtail** — thành phần vận chuyển log từ Docker host vào Loki và gán nhãn `container_name` để Grafana filter theo container.

## Ý nghĩa từng phần

### Causal chain / luồng dữ liệu (tương ứng với use case 3 trong README vừa viết)

1. **Nguồn dữ liệu**: Docker daemon (`json-file` log driver theo config `/etc/docker/daemon.json` trong README) viết log container ra host path `/var/lib/docker/containers/<id>/<id>-json.log` — mỗi dòng là JSON `{log, stream, time}`.
2. **Discovery**: Promtail dùng `docker_sd_configs` → nối `unix:///var/run/docker.sock` → quét danh sách container đang chạy (refresh 5s) → nhận `__meta_docker_container_name`, `__meta_docker_container_log_stream` (và các metadata khác).
3. **Relabel** (chọn/đổi nhãn cho series): có 2 rule:
   - Rule 1: lấy `__meta_docker_container_name` (dạng `/coder-mike-kuma`) → regex `(.+)` → gắn vào nhãn mới `container_name` (cắt bỏ `/`) → nhãn này sẽ kèm theo mọi log stream từ container đó.
   - Rule 2: lấy `__meta_docker_container_log_stream` → tặng nhãn `stream` (thường `stdout` hoặc `stderr`) → hữu ích filter theo stream.
4. **Pipeline** `docker: {}` **mở envelope JSON**: dữ liệu thô từ file log từng dòng là `{..., "log": "<actual logs>", "stream": "stdout", "time": ...}`. Rule `docker: {}` đọc JSON, trả về 3 trường: `log` (text thật), `stream`, `time` → sau đó nhãn `container_name`, `stream` từ relabel được giữ. Sau này Grafana lúc query `/loki/api/v1/sign...} pipeline` sẽ dùng label này filter.
5. **Push đến Loki**: `clients: url: http://loki:3100/loki/api/v1/push` — Promtail gửi log sang Loki (compose network: service `loki` tên `loki`, port 3100).
6. **Persistence của tiến trình**: `positions: filename: /positions/positions.yaml` — Promtail lưu state đã đọc đến đâu của từng stream ở file này (bind mount trong compose). Assist crash/restart không đọc lại từ đầu.

### Tại sao lại cần relabel + docker pipeline này

- Sau bước relabel, log stream được gắn label `container_name` (ví dụ `coder-mike-kuma`), `stream` (stdout/stderr).
- Sau bước `docker: {}`, nội dung log thật được trích xuất từ envelope JSON → Grafana Logs panel hiển thị dòng text (không phải toàn bộ JSON).
- Query ở Grafana: `{container_name="coder-mike-kuma"}` hoặc `{container_name=~"coder-mike.*"}` — trích log từng user workspace.

### Compose binding (ẩn trong docker-compose.yml)

Mình đã mount:
- `/var/run/docker.sock` → Promtail 접근 Docker daemon (cần cho `docker_sd_configs`).
- `/var/lib/docker/containers` → Promtail đọc file log JSON.
- `/positions` →持久化 của Promtail.

### Lưu ý thêm

- Log driver Docker phải là `json-file` (không `journald`/`local`) và có `tag` để Promtail biết tên container theo way cũ, tuy nhiên Promtail `docker_sd` dùng tên container thực tế nên không phụ thuộc `tag` của Docker — nhưng container name trong docker là `/coder-mike-kuma`, regex `/(.+)` cắt bỏ `/` để nhận `coder-mike-kuma`.
- `refresh_interval: 5s` cho discovery — container mới start sau 5s Promtail sẽ bắt diện tử tail.

Tóm lại, file này định nghĩa cách Promtail **phát hiện**, **lọc**, **mở envelope**, **gắn nhãn container_name**, và **gửi** log Docker sang Loki để Grafana readable + filter theo user workspace.

Nếu cần giải thích thêm về pipeline stages nâng cao (ví dụ parse log line thành structured fields bằng regex/Lua) hoặc flow error (Promtail logs, retry), mình có thể viết thêm.