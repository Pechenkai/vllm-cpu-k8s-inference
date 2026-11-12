"""Скачивание фиксированной ревизии модели HF и размещение на общем томе."""

import fcntl
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import shutil
import sys


LOGGER = logging.getLogger("model_downloader")
MARKER = ".download-complete.json"
REPO_PATTERN = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]*(/[A-Za-z0-9_][A-Za-z0-9_.-]*)?")


class DownloadError(Exception):
    """Ошибка настроек или кэша, которую можно сообщить без раскрытия учётных данных."""


def model_directory(root: Path, model_id: str, revision: str) -> Path:
    if (
        not REPO_PATTERN.fullmatch(model_id)
        or ".." in model_id
        or "--" in model_id
        or model_id.endswith((".", "-"))
    ):
        raise DownloadError("MODEL_ID must be a Hugging Face model repository ID")
    if not re.fullmatch(r"[0-9a-f]{40}", revision):
        raise DownloadError(
            "MODEL_REVISION must be a full lowercase 40-character commit SHA"
        )
    return root / ("models--" + model_id.replace("/", "--")) / revision


def inventory(directory: Path) -> dict:
    """Хеширование файлов модели без служебных метаданных о завершении загрузки."""
    result = {}
    for path in sorted(directory.rglob("*")):
        relative = path.relative_to(directory)
        if relative.as_posix() == MARKER:
            continue
        if path.is_symlink():
            raise DownloadError("Model payload must not contain symbolic links")
        if path.is_dir():
            continue
        if not path.is_file():
            raise DownloadError("Model payload contains a non-regular file")
        with path.open("rb") as stream:
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
        result[relative.as_posix()] = {"size": path.stat().st_size, "sha256": digest}
    return result


def verify_complete(directory: Path, model_id: str, revision: str) -> None:
    try:
        manifest = json.loads((directory / MARKER).read_text())
    except (OSError, ValueError) as error:
        raise DownloadError(
            "Existing model has no valid completion manifest"
        ) from error
    if not isinstance(manifest, dict) or (
        manifest.get("schema") != 1
        or manifest.get("model_id") != model_id
        or manifest.get("revision") != revision
    ):
        raise DownloadError(
            "Existing model manifest does not match the requested revision"
        )
    files = manifest.get("files")
    if not isinstance(files, dict) or not files or files != inventory(directory):
        raise DownloadError("Existing model is incomplete or its files have changed")


def fetch_snapshot(model_id: str, revision: str, directory: Path) -> None:
    # Импортируем только при промахе кэша; готовая модель не требует HF API и HTTP.
    from huggingface_hub import snapshot_download

    snapshot_download(
        repo_id=model_id,
        revision=revision,
        local_dir=directory,
        token=os.environ.get("HF_TOKEN") or False,
        max_workers=2,
    )


def prepare_model(root: Path, model_id: str, revision: str) -> Path:
    destination = model_directory(root, model_id, revision)
    destination.parent.mkdir(parents=True, exist_ok=True)

    # Один писатель на репозиторий, в том числе при одновременном старте Pod.
    # Нужен POSIX-том с flock и атомарным переименованием в одной файловой системе.
    with (destination.parent / ".download.lock").open("a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if destination.exists():
            verify_complete(destination, model_id, revision)
            LOGGER.info(
                "cache_hit model=%s revision=%s path=%s",
                model_id,
                revision,
                destination,
            )
            return destination

        staging = destination.parent / f".{revision}.partial"
        staging.mkdir(exist_ok=True)
        # Сбой до переименования может оставить старый маркер во временной папке.
        (staging / MARKER).unlink(missing_ok=True)
        LOGGER.info("download_started model=%s revision=%s", model_id, revision)
        fetch_snapshot(model_id, revision, staging)

        # Метаданные HF local_dir позволяют продолжать прерванную загрузку.
        # После полного скачивания они не нужны процессу инференса.
        metadata = staging / ".cache" / "huggingface"
        if metadata.exists():
            shutil.rmtree(metadata)
        files = inventory(staging)
        if "config.json" not in files or not any(
            name.endswith((".safetensors", ".bin")) for name in files
        ):
            raise DownloadError("Downloaded repository lacks model config or weights")
        manifest = {
            "schema": 1,
            "model_id": model_id,
            "revision": revision,
            "files": files,
        }
        with (staging / MARKER).open("w") as stream:
            json.dump(manifest, stream, sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
        # Публикуем только полный каталог; прежняя ревизия не заменяется.
        os.replace(staging, destination)
        LOGGER.info("download_complete path=%s", destination)
        return destination


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    # Отключаем отладочный вывод HF в формате curl с заголовками авторизации.
    os.environ["HF_DEBUG"] = "0"
    os.environ["HF_HUB_DISABLE_PROGRESS_BARS"] = "1"
    try:
        root = Path(os.environ.get("MODEL_ROOT", "/models"))
        path = prepare_model(
            root, os.environ.get("MODEL_ID", ""), os.environ.get("MODEL_REVISION", "")
        )
        print(path, flush=True)
        return 0
    except DownloadError as error:
        print(f"Model preparation failed: {error}", file=sys.stderr)
    except Exception as error:
        # Исключения HTTP могут содержать приватные данные. Сообщаем только тип;
        # временные файлы оставляем для повтора, без маркера готовности.
        print(
            f"Model preparation failed ({type(error).__name__}); retry is safe",
            file=sys.stderr,
        )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
