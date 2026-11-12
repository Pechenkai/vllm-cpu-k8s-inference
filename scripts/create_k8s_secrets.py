"""Создание Secret без передачи учётных данных в аргументах команд или файлах."""

import argparse
import json
import os
import secrets
import subprocess
import sys


def create_if_missing(kubectl: list[str], name: str, key: str, value: str) -> None:
    existing = subprocess.run(
        [*kubectl, "get", "secret", name, "--ignore-not-found", "-o", "name"],
        check=True,
        capture_output=True,
        text=True,
    )
    if existing.stdout.strip():
        print(f"Secret {name} already exists; left unchanged")
        return
    payload = {
        "apiVersion": "v1",
        "kind": "Secret",
        "metadata": {"name": name},
        "type": "Opaque",
        "stringData": {key: value},
    }
    # Учётные данные передаются через stdin, без аргументов команд и файлов.
    subprocess.run(
        [*kubectl, "create", "-f", "-"],
        input=json.dumps(payload),
        text=True,
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    print(f"Secret {name} created")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--context", default="kaspersky")
    parser.add_argument("--namespace", default="kaspersky-llm")
    args = parser.parse_args()
    kubectl = ["kubectl", "--context", args.context, "--namespace", args.namespace]
    try:
        create_if_missing(
            kubectl,
            "vllm-api",
            "api-key",
            os.environ.get("VLLM_API_KEY") or secrets.token_urlsafe(32),
        )
        if token := os.environ.get("HF_TOKEN"):
            create_if_missing(kubectl, "huggingface-token", "token", token)
        else:
            print("HF_TOKEN not provided; public model can be downloaded without it")
    except (OSError, subprocess.CalledProcessError):
        print(
            "Could not create Secrets; check context, namespace and permissions",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
