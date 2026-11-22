#!/usr/bin/env bash
# First command in a new Claude Code cloud session (runs as root in the container).
# Idempotent: every step is skipped when it is already done, so it is safe to run again.
#
#   bash scripts/cloud-setup.sh            # everything, including all task images
#   bash scripts/cloud-setup.sh --no-images  # skip the SWE-rebench task images (~5 GB each)
#
# Afterwards run Mergewright and the tests as the non-root user `mw` (see the end of this file).
# Why: as root, the sandbox runs as `nobody` (src/mergewright/sandbox/docker.py, _default_user),
# and `nobody` cannot write the root-owned workspace.
#
# Needs the environment's allowed hosts `datasets-server.huggingface.co` (task import) and
# `deb.debian.org` (image build); Docker Hub is reachable by default.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PULL_IMAGES=1
[[ "${1:-}" == "--no-images" ]] && PULL_IMAGES=0
log() { printf '\n== %s\n' "$*"; }

log "Docker daemon"
if ! docker info >/dev/null 2>&1; then
  nohup dockerd >/var/log/dockerd.log 2>&1 &
  for _ in $(seq 1 30); do docker info >/dev/null 2>&1 && break; sleep 1; done
  docker info >/dev/null 2>&1 || { echo "dockerd did not start; see /var/log/dockerd.log"; exit 1; }
fi
echo "running: $(docker info --format '{{.ServerVersion}}')"

log "Non-root user mw (member of the docker group)"
getent group docker >/dev/null || groupadd docker
id mw >/dev/null 2>&1 || useradd -m -G docker mw
chgrp docker /var/run/docker.sock && chmod 660 /var/run/docker.sock
# `su mw` keeps SSL_CERT_FILE etc., which point at the proxy's CA under /root. It is a public
# certificate, not a secret: let mw read it (the OpenAI client loads it when it is built).
chmod o+x /root /root/.ccr && chmod o+r /root/.ccr/ca-bundle.crt

log "Python venv and package"
# As root: pip needs the proxy CA under /root, which mw cannot read. The chown below fixes
# ownership.
(cd "$REPO" && { [ -d .venv ] || python3 -m venv .venv; } \
  && .venv/bin/pip install -q -e '.[dev]')

# Files the root agent writes later are root-owned: run this script again if mw gets EACCES.
chown -R mw:mw "$REPO"
git config --global --get-all safe.directory | grep -qx "$REPO" \
  || git config --global --add safe.directory "$REPO"

log "Sandbox image mergewright-python:1"
if ! docker image inspect mergewright-python:1 >/dev/null 2>&1; then
  # docker/python/Dockerfile cannot run here unchanged: build containers do not get the
  # session proxy, and the proxy serves HTTPS only (apt defaults to HTTP). This build installs
  # the same packages through the proxy and leaves no proxy setting or CA in the image.
  base=$(grep -m1 '^FROM ' "$REPO/docker/python/Dockerfile" | cut -d' ' -f2)
  pytest_pin=$(grep -o 'pytest==[0-9.]*' "$REPO/docker/python/Dockerfile")
  ctx=$(mktemp -d)
  cp /root/.ccr/ca-bundle.crt "$ctx/ca.crt"
  cat >"$ctx/Dockerfile" <<EOF
FROM $base
ARG https_proxy
COPY ca.crt /tmp/ca.crt
RUN sed -i 's#http://deb.debian.org#https://deb.debian.org#g' /etc/apt/sources.list.d/debian.sources \\
    && printf 'Acquire::https::Proxy "%s";\nAcquire::https::CAInfo "/tmp/ca.crt";\n' "\$https_proxy" > /etc/apt/apt.conf.d/99proxy \\
    && apt-get update \\
    && apt-get install -y --no-install-recommends git \\
    && rm -rf /var/lib/apt/lists/* /etc/apt/apt.conf.d/99proxy \\
    && sed -i 's#https://deb.debian.org#http://deb.debian.org#g' /etc/apt/sources.list.d/debian.sources
RUN PIP_CERT=/tmp/ca.crt pip install --no-cache-dir $pytest_pin && rm /tmp/ca.crt
EOF
  docker build -q --network host --build-arg "https_proxy=$HTTPS_PROXY" \
    -t mergewright-python:1 "$ctx"
  rm -rf "$ctx"
fi
echo "ready: mergewright-python:1"

if [[ $PULL_IMAGES == 1 ]]; then
  log "Task images (pinned by digest, tagged :latest because the tests look them up by name)"
  for task in "$REPO"/eval/tasks/*/task.json; do
    image=$(python3 -I -c 'import json, sys; print(json.load(open(sys.argv[1]))["image"])' "$task")
    if ! docker image inspect "$image" >/dev/null 2>&1; then
      echo "pull $image"
      docker pull -q "$image" >/dev/null
    fi
    docker tag "$image" "${image%@*}:latest"
  done
  df -h / | tail -1
fi

log "Done. Run as mw, from the repo:"
cat <<EOF
  su mw -s /bin/bash -c '. .venv/bin/activate && ruff check . && pytest -q'
  su mw -s /bin/bash -c '. .venv/bin/activate && mergewright tasks validate'
Known on this host: test_cpu_memory_and_pid_limits_are_set_in_the_cgroup fails (cgroup v1 here;
the test reads cgroup v2 files). The memory and PID limit behavior tests pass.
EOF
