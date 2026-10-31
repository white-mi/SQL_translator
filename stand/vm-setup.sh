#!/usr/bin/env bash
# One-time setup of a fresh Ubuntu 24.04 VM: Docker Engine, compose plugin, Python tooling.
# Run as a sudo-capable user: bash stand/vm-setup.sh
set -euo pipefail

sudo apt-get update
sudo apt-get install -y ca-certificates curl git python3-venv python3-pip jq

# Docker Engine from the official repository (https://docs.docker.com/engine/install/ubuntu/)
sudo install -m 0755 -d /etc/apt/keyrings
sudo curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
sudo chmod a+r /etc/apt/keyrings/docker.asc
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] \
https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo "$VERSION_CODENAME") stable" \
  | sudo tee /etc/apt/sources.list.d/docker.list >/dev/null
sudo apt-get update
sudo apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
sudo usermod -aG docker "$USER"

# uv for reproducible Python environments (the arena uses it too)
curl -LsSf https://astral.sh/uv/install.sh | sh

echo "Done. Log out and back in so the docker group applies, then run: bash stand/bootstrap.sh"
