FROM python:3.11-slim

ARG DOCKER_VERSION=28.2.2
ARG HELM_VERSION=v3.16.2
ARG KIND_VERSION=v0.29.0
ARG KUBECTL_VERSION=v1.32.2

RUN apt-get update \
    && apt-get install -y --no-install-recommends bash ca-certificates curl git jq tar gzip \
    && rm -rf /var/lib/apt/lists/*

RUN arch="$(dpkg --print-architecture)" \
    && case "$arch" in \
        amd64) docker_arch=x86_64; helm_arch=amd64; kind_arch=amd64; kubectl_arch=amd64 ;; \
        arm64) docker_arch=aarch64; helm_arch=arm64; kind_arch=arm64; kubectl_arch=arm64 ;; \
        *) echo "unsupported architecture: $arch" >&2; exit 1 ;; \
    esac \
    && curl -fsSL "https://download.docker.com/linux/static/stable/${docker_arch}/docker-${DOCKER_VERSION}.tgz" \
        | tar -xz --strip-components=1 -C /usr/local/bin docker/docker \
    && curl -fsSL -o /usr/local/bin/kubectl \
        "https://dl.k8s.io/release/${KUBECTL_VERSION}/bin/linux/${kubectl_arch}/kubectl" \
    && chmod +x /usr/local/bin/kubectl \
    && curl -fsSL -o /usr/local/bin/kind \
        "https://kind.sigs.k8s.io/dl/${KIND_VERSION}/kind-linux-${kind_arch}" \
    && chmod +x /usr/local/bin/kind \
    && curl -fsSL "https://get.helm.sh/helm-${HELM_VERSION}-linux-${helm_arch}.tar.gz" \
        | tar -xz -C /tmp \
    && mv "/tmp/linux-${helm_arch}/helm" /usr/local/bin/helm \
    && chmod +x /usr/local/bin/helm \
    && rm -rf /tmp/"linux-${helm_arch}"

RUN curl -fsSL https://raw.githubusercontent.com/NVIDIA/OSMO/main/install.sh | bash

WORKDIR /workspace/roar

CMD ["bash", "-lc", "sleep infinity"]
