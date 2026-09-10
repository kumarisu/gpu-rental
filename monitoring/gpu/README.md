# Optional host-side NVIDIA GPU exporter
#
# The exporter must run ON THE HOST (not in a container): only the host can run
# `nvidia-smi` and map PIDs back to Coder workspace containers via the Docker
# socket.
#
#   1. Install Python deps:
#         pip install prometheus-client
#
#   2. Test it:
#         python3 host_gpu_exporter.py          # listens on :9091
#         curl -s localhost:9091/metrics | grep gpu
#
#   3. Install as a systemd service (recommended, auto-restarts):
#         sudo mkdir -p /opt/gpu-rental-infra
#         sudo cp -r ../../../.. /opt/gpu-rental-infra   # or symlink the repo
#         sudo cp gpu-rental-gpu-exporter.service /etc/systemd/system/
#         sudo systemctl daemon-reload
#         sudo systemctl enable --now gpu-rental-gpu-exporter
#
#   4. Uncomment the `gpu-exporter` scrape job in
#      ../prometheus/prometheus.yml  (target host.docker.internal:9091) and
#      reload Prometheus.
#
# Metrics / billing:
#   * host-level:   gpu_utilization_percent, gpu_memory_used_bytes
#   * per workspace container (via PID mapping + coder.owner label):
#                   gpu_container_memory_used_bytes{container_name=..., owner=...}
#
# To charge per GPU-second, extend billing/sync/sync.py with a `gpu_seconds`
# billable metric computed from gpu_utilization_percent * window (see the
# dashboard panel "GPU utilization %").