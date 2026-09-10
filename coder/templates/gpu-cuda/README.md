---
name: gpu-cuda
display_name: GPU Workspace (CUDA)
description: NVIDIA-GPU Docker workspace for ML/AI workloads (CUDA 12.4 base image).
tags: [gpu, nvidia, cuda, docker]
---

# GPU Workspace (CUDA)

Docker-provisioned workspace with access to the host GPUs (`gpus = "all"`).

- Image: `gpu-rental/ws-gpu:latest` (build with `make template-images`)
- Persistent home volume per workspace
- Container exposes the `coder.owner` / `coder.workspace_id` labels used by the
  monitoring & billing pipeline (cAdvisor → Prometheus → billing-sync → Lago)