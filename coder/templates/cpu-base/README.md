---
name: cpu-base
display_name: CPU Workspace (Base)
description: Lightweight CPU-only Docker workspace for general development.
tags: [cpu, docker, development]
---

# CPU Workspace (Base)

Docker-provisioned CPU workspace.

- Image: `gpu-rental/ws-cpu:latest` (build with `make template-images`)
- Persistent home volume per workspace
- Container exposes the `coder.owner` / `coder.workspace_id` labels used by the
  monitoring & billing pipeline (cAdvisor → Prometheus → billing-sync → Lago)