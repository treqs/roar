FROM rayproject/ray:2.44.1-py312

USER root

RUN apt-get update \
    && apt-get install -y --no-install-recommends curl ca-certificates build-essential \
    && rm -rf /var/lib/apt/lists/*

ENV RUSTUP_HOME=/root/.rustup
ENV CARGO_HOME=/root/.cargo
ENV PATH=/root/.cargo/bin:${PATH}

RUN curl https://sh.rustup.rs -sSf | sh -s -- -y --profile minimal --default-toolchain stable

WORKDIR /app

COPY . /app

RUN pip install --no-cache-dir boto3 pyarrow pandas pytest pytest-timeout minio \
    && pip install --no-cache-dir -e .
