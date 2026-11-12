"""Настройка защиты логов до импортов в дочерних Python-процессах vLLM."""

import os

if os.environ.get("PRIVATE_LOGGING_ACTIVE") == "1":
    try:
        from app.private_logging import install

        install()
    except Exception:
        # Python обычно игнорирует ошибки sitecustomize и продолжает работу.
        # Здесь завершаем процесс: работа с незащищённым выводом недопустима.
        os._exit(78)
