"""Запуск vLLM с обязательной защитой логов, включая дочерние процессы."""

import logging.config
import os
from pathlib import Path
import sys
import json

from app.private_logging import install

CONFIG = str(Path(__file__).with_name("logging.json"))


def main():
    install()
    os.environ["VLLM_LOGGING_CONFIG_PATH"] = CONFIG
    os.environ["VLLM_CONFIGURE_LOGGING"] = "1"
    os.environ["VLLM_DEBUG_LOG_API_SERVER_RESPONSE"] = "0"
    os.environ["HF_DEBUG"] = "0"
    with open(CONFIG) as source:
        logging.config.dictConfig(json.load(source))
    if not os.environ.get("VLLM_API_KEY"):
        logging.getLogger(__name__).error("API key missing")
        return 2
    # CLI имеет приоритет над YAML; защита сохраняется даже при замене CMD.
    sys.argv = [
        "vllm",
        "serve",
        *sys.argv[1:],
        "--no-enable-log-requests",
        "--no-enable-log-outputs",
        "--no-log-error-stack",
        "--log-config-file",
        CONFIG,
        "--middleware",
        "app.audit.AuditMiddleware",
    ]
    from vllm.entrypoints.cli.main import main as vllm_main

    vllm_main()
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception:
        logging.getLogger(__name__).exception("Server failed")
        raise SystemExit(1) from None
