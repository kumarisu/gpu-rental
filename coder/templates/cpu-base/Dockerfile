# CPU workspace image for the GPU Rental platform.
#
#   docker build -t gpu-rental/ws-cpu:latest coder/templates/cpu-base
FROM ubuntu:22.04

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

CMD ["/bin/bash", "-l"]