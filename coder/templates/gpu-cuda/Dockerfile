# GPU workspace image for the GPU Rental platform.
#
#   docker build -t gpu-rental/ws-gpu:latest coder/templates/gpu-cuda
#
# The host must have the NVIDIA driver installed; the container exposes the
# driver's shared libraries automatically when Coder starts it with gpus = "all".
FROM nvidia/cuda:12.4.1-base-ubuntu22.04

ENV DEBIAN_FRONTEND=noninteractive \
    LANG=C.UTF-8 \
    LC_ALL=C.UTF-8

RUN apt-get update && apt-get install -y --no-install-recommends \
      curl \
      ca-certificates \
      git \
      sudo \
      htop \
      python3 \
      python3-pip \
      build-essential \
    && rm -rf /var/lib/apt/lists/*

# Coder user (container keeps running as root so workspaces can install things).
RUN useradd -m -s /bin/bash -u 1000 coder \
    && echo "coder  ALL=(ALL) NOPASSWD:ALL" >> /etc/sudoers.d/coder

# Common ML tooling (uncomment / extend as needed):
# RUN pip3 install --no-cache-dir torch torchvision --index-url \
#     https://download.pytorch.org/whl/cu124

CMD ["/bin/bash", "-l"]