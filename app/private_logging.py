"""Логирование разрешённых полей; сырой stdout/stderr направляется в /dev/null.

Через Linux /proc дочерние процессы открывают исходный дескриптор вывода API.
В него пишет только SafeHandler. Сообщения и исключения не форматируются.
"""

import json
import logging
import os
import sys
from datetime import datetime, timezone

_output = None


def install() -> None:
    global _output
    if _output is not None:
        return
    parent_output = os.environ.get("PRIVATE_LOG_OUTPUT")
    descriptor = os.open(parent_output, os.O_WRONLY) if parent_output else os.dup(1)
    _output = os.fdopen(descriptor, "w", buffering=1)
    if not parent_output:
        os.environ["PRIVATE_LOG_OUTPUT"] = f"/proc/{os.getpid()}/fd/{descriptor}"
    os.environ["PRIVATE_LOGGING_ACTIVE"] = "1"
    # Исключаем из логов произвольный print(), нативный вывод и traceback
    # дочерних процессов. Сохранённый дескриптор открыт до завершения процесса.
    sys.stdout.flush()
    sys.stderr.flush()
    with open(os.devnull, "w") as sink:
        os.dup2(sink.fileno(), 1)
        os.dup2(sink.fileno(), 2)
    logging.raiseExceptions = False


class SafeFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        result = {
            "time": datetime.fromtimestamp(record.created, timezone.utc).isoformat(),
            "level": logging.getLevelName(record.levelno),
            "pid": record.process,
        }
        # Поля аудита формирует наш middleware. Не копируем произвольные extra, args,
        # текст исключений, ID клиента, URL, параметры, заголовки или всё сообщение.
        if record.name == "private_audit" and record.msg == "request_finished":
            result.update(event="request_finished", **record.audit)
        else:
            result.update(
                event="diagnostic",
                logger=record.name,
                line=record.lineno,
                exception=bool(record.exc_info),
            )
        return json.dumps(result, ensure_ascii=True, separators=(",", ":"))


class SafeHandler(logging.StreamHandler):
    def __init__(self):
        install()
        super().__init__(_output)
