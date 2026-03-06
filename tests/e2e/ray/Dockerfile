FROM rayproject/ray:2.54.0-py312

USER root

RUN apt-get update \
    && apt-get install -y --no-install-recommends curl ca-certificates build-essential \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY . /app

RUN pip install --no-cache-dir \
    pydantic==2.12.5 \
    pydantic-settings==2.12.0 \
    boto3 pyarrow pandas pytest pytest-timeout minio \
    && pip install --no-cache-dir -e .
