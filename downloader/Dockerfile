FROM python:3.12.13-slim-bookworm@sha256:4766d8b510c428e595d74b9cc5bbb2fae8e26316fffb4adc89908d79aacd58a2

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    HOME=/home/vllm \
    HF_HOME=/home/vllm/.cache/huggingface \
    HF_HUB_DISABLE_PROGRESS_BARS=1 \
    MODEL_ROOT=/models \
    MODEL_ID=Qwen/Qwen2.5-0.5B-Instruct \
    MODEL_REVISION=7ae557604adf67be50417f59c2c2f167def9a775

COPY requirements.txt /opt/downloader/requirements.txt
RUN python -m pip install --no-cache-dir --only-binary=:all: --require-hashes \
        --index-url https://pypi.org/simple -r /opt/downloader/requirements.txt \
    && python -m pip check \
    && groupadd --gid 10001 vllm \
    && useradd --uid 10001 --gid 10001 --create-home \
        --home-dir /home/vllm --shell /usr/sbin/nologin vllm \
    && mkdir -p /models /home/vllm/.cache \
    && chown -R 10001:10001 /models /home/vllm

COPY download_model.py /opt/downloader/download_model.py
WORKDIR /opt/downloader
USER 10001:10001
ENTRYPOINT ["python", "/opt/downloader/download_model.py"]
