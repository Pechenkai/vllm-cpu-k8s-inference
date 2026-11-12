FROM python:3.12.13-slim-bookworm@sha256:4766d8b510c428e595d74b9cc5bbb2fae8e26316fffb4adc89908d79aacd58a2

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    VIRTUAL_ENV=/opt/venv \
    PATH="/opt/venv/bin:$PATH"

RUN test "$(dpkg --print-architecture)" = amd64 \
    && apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        g++ \
        libatomic1 \
        libgomp1 \
        libnuma1 \
        libsndfile1 \
        libstdc++6 \
        libtcmalloc-minimal4 \
    && rm -rf /var/lib/apt/lists/* \
    && python -m venv /opt/venv

COPY requirements.txt /opt/app/requirements.txt

RUN python -m pip install --no-cache-dir --require-hashes \
        --index-url https://pypi.org/simple \
        --extra-index-url https://download.pytorch.org/whl/cpu \
        -r /opt/app/requirements.txt \
    && python -m pip check \
    && test -r /opt/venv/lib/libiomp5.so

RUN groupadd --gid 10001 vllm \
    && useradd --uid 10001 --gid 10001 --create-home \
        --home-dir /home/vllm --shell /usr/sbin/nologin vllm \
    && mkdir -p /models /home/vllm/.cache \
    && chown -R 10001:10001 /models /home/vllm

ENV HOME=/home/vllm \
    HF_HOME=/models/huggingface \
    XDG_CACHE_HOME=/home/vllm/.cache \
    LD_PRELOAD="/usr/lib/x86_64-linux-gnu/libtcmalloc_minimal.so.4:/opt/venv/lib/libiomp5.so" \
    VLLM_CPU_KVCACHE_SPACE=1 \
    VLLM_CPU_OMP_THREADS_BIND=nobind \
    OMP_NUM_THREADS=4

COPY app /opt/app/app
COPY sitecustomize.py /opt/app/sitecustomize.py
ENV PYTHONPATH=/opt/app

WORKDIR /opt/app
USER 10001:10001

EXPOSE 8000
ENTRYPOINT ["python", "-m", "app.serve"]
CMD ["Qwen/Qwen2.5-0.5B-Instruct", "--host", "0.0.0.0", "--port", "8000", "--dtype", "float32", "--max-model-len", "1024", "--max-num-seqs", "1", "--max-num-batched-tokens", "1024", "--enforce-eager", "--no-enable-log-requests", "--no-enable-log-outputs"]
