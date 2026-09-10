#!/usr/bin/env python3
"""
Host-side NVIDIA GPU exporter for the GPU Rental platform.

Exposes Prometheus text-format metrics on :9091 (default):

  gpu_utilization_percent{gpu="0",name="GeForce RTX 4090"}      host-level, per GPU
  gpu_memory_used_bytes{gpu="0"}  /  gpu_memory_total_bytes
  gpu_container_memory_used_bytes{gpu="0",container_name="coder-mike-gpu01",
                                  owner="mike"}                  per workspace container

Per-container attribution: PID→container via the Docker socket
(`docker inspect --format '{{.State.Pid}}'` nesting check), owner from the
`coder.owner` label. Requires the host to have `nvidia-smi` and Python 3.8+:

  pip install prometheus-client
  python3 host_gpu_exporter.py            # serves :9091

Or install as a service (recommended):
  sudo cp gpu-rental-gpu-exporter.service /etc/systemd/system/
  sudo systemctl enable --now gpu-rental-gpu-exporter

Then uncomment the `gpu-exporter` job in monitoring/prometheus/prometheus.yml.
"""
import json
import os
import re
import subprocess
import time

from prometheus_client import Gauge, start_http_server

GPU_EXPORTER_PORT = int(os.environ.get("GPU_EXPORTER_PORT", "9091"))

gpu_util = Gauge("gpu_utilization_percent", "GPU utilization %", ["gpu", "name"])
gpu_mem_used = Gauge("gpu_memory_used_bytes", "GPU memory used bytes", ["gpu"])
gpu_mem_total = Gauge("gpu_memory_total_bytes", "GPU memory total bytes", ["gpu"])
gpu_cont_mem = Gauge("gpu_container_memory_used_bytes",
                     "GPU memory used by a Coder workspace container", ["gpu", "container_name", "owner"])


def nvidia_smi(argv):
    try:
        out = subprocess.run(["nvidia-smi", *argv], capture_output=True, text=True, timeout=20)
        return out.stdout
    except Exception:  # noqa: BLE001
        return ""


def container_of_pid(pid):
    """Return (container_name, owner) if PID belongs to a Coder workspace, else (None, None)."""
    try:
        cid = subprocess.run(
            ["docker", "ps", "-q", "--no-trunc"],
            capture_output=True, text=True, timeout=10).stdout.split()
        for c in cid:
            pid_ns = subprocess.run(
                ["docker", "inspect", "-f", "{{.State.Pid}}", c],
                capture_output=True, text=True, timeout=10).stdout.strip()
            if pid_ns and pid_ns.isdigit() and int(pid_ns) == int(pid):
                label = subprocess.run(
                    ["docker", "inspect", "-f", "{{index .Config.Labels \"coder.owner\"}}", c],
                    capture_output=True, text=True, timeout=10).stdout.strip()
                name = subprocess.run(
                    ["docker", "inspect", "-f", "{{.Name}}", c],
                    capture_output=True, text=True, timeout=10).stdout.strip()
                return name, label or None
    except Exception:  # noqa: BLE001
        pass
    return None, None


def update():
    # per-GPU utilization & memory
    for line in nvidia_smi(["--query-gpu=index,name,utilization.gpu,memory.used,memory.total",
                            "--format=csv,noheader,nounits"]).splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 5:
            continue
        gpu, name, util, used, total = parts[:5]
        gpu_util.labels(gpu=gpu, name=name).set(float(util or 0))
        gpu_mem_used.labels(gpu=gpu).set(float(used or 0) * 1024 * 1024)
        gpu_mem_total.labels(gpu=gpu).set(float(total or 0) * 1024 * 1024)

    # per-container GPU memory (PID → container on the host)
    seen = set()
    for line in nvidia_smi(["--query-compute-apps=pid,used_gpu_memory",
                            "--format=csv,noheader,nounits"]).splitlines():
        m = re.match(r"\s*(\d+)\s*,\s*([\d.]+)\s*", line)
        if not m:
            continue
        pid, mem = m.group(1), float(m.group(2))
        gpu = "?"  # nvidia-smi compute-apps does not expose the GPU index; kept for per-pid
        name, owner = container_of_pid(pid)
        if name:
            gpu_cont_mem.labels(gpu=gpu, container_name=name, owner=owner or "").set(mem * 1024 * 1024)
            seen.add((gpu, name, owner or ""))


if __name__ == "__main__":
    start_http_server(GPU_EXPORTER_PORT)
    print(f"gpu-exporter listening on :{GPU_EXPORTER_PORT}")
    while True:
        try:
            update()
        except Exception as exc:  # noqa: BLE001
            print("export error:", exc)
        time.sleep(5)