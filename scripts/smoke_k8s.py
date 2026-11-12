"""Проверка Service, Bearer-авторизации и потоковых ответов через port-forward."""

import argparse
import base64
import json
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request


def check_api(base_url: str, token: str, model: str) -> None:
    body = {
        "model": model,
        "messages": [{"role": "user", "content": "Say hello."}],
        "max_tokens": 8,
        "temperature": 0.7,
    }
    url = base_url + "/v1/chat/completions"

    def request(bearer, payload):
        headers = {"Content-Type": "application/json"}
        if bearer is not None:
            headers["Authorization"] = "Bearer " + bearer
        return urllib.request.urlopen(
            urllib.request.Request(
                url, data=json.dumps(payload).encode(), headers=headers
            ),
            timeout=180,
        )

    for bearer in (None, token + "-invalid"):
        try:
            with request(bearer, body):
                raise RuntimeError("Unauthenticated request unexpectedly succeeded")
        except urllib.error.HTTPError as error:
            if error.code != 401:
                raise RuntimeError("Expected HTTP 401") from error
    print("Missing/wrong API token: HTTP 401")

    with request(token, body) as response:
        result = json.load(response)
        if response.status != 200 or result["usage"]["completion_tokens"] <= 0:
            raise RuntimeError("Expected generated output and HTTP 200")
    print("Authorized sampling request: HTTP 200")

    content_received = False
    done_received = False
    with request(token, {**body, "stream": True}) as response:
        for raw in response:
            line = raw.decode().strip()
            if not line.startswith("data: "):
                continue
            data = line[6:]
            if data == "[DONE]":
                done_received = True
                break
            for choice in json.loads(data).get("choices", []):
                content_received |= bool(choice.get("delta", {}).get("content"))
    if not (content_received and done_received):
        raise RuntimeError("Streaming response was incomplete")
    print("Streaming request: generated text and [DONE] received")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--context", default="kaspersky")
    parser.add_argument("--namespace", default="kaspersky-llm")
    parser.add_argument("--model", default="Qwen/Qwen2.5-0.5B-Instruct")
    parser.add_argument(
        "--privacy",
        action="store_true",
        help="Also inspect Pod logs for sensitive content",
    )
    args = parser.parse_args()
    kubectl = ["kubectl", "--context", args.context, "-n", args.namespace]
    forwarding = None
    try:
        secret = json.loads(
            subprocess.check_output(
                [*kubectl, "get", "secret", "vllm-api", "-o", "json"]
            )
        )
        token = base64.b64decode(secret["data"]["api-key"], validate=True).decode()
        if not token:
            raise RuntimeError("API token is empty")
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        forwarding = subprocess.Popen(
            [
                *kubectl,
                "port-forward",
                "service/vllm",
                f"{port}:8000",
                "--address=127.0.0.1",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        base_url = f"http://127.0.0.1:{port}"
        for _ in range(60):
            if forwarding.poll() is not None:
                raise RuntimeError(
                    "Port-forward exited; check Service and pod readiness"
                )
            try:
                with urllib.request.urlopen(base_url + "/health", timeout=2):
                    break
            except OSError:
                time.sleep(0.5)
        else:
            raise RuntimeError("Service did not become reachable")
        check_api(base_url, token, args.model)
        if args.privacy:
            from privacy_check import PrivacyCheckError, check_privacy

            def read_logs():
                return subprocess.check_output(
                    [*kubectl, "logs", "deployment/vllm", "-c", "vllm"],
                    text=True,
                )

            try:
                check_privacy(base_url, token, args.model, read_logs)
            except PrivacyCheckError as error:
                print(f"Privacy check failed: {error}", file=sys.stderr)
                return 1
        return 0
    except (
        OSError,
        ValueError,
        KeyError,
        RuntimeError,
        subprocess.CalledProcessError,
    ) as error:
        print(f"Smoke check failed: {type(error).__name__}", file=sys.stderr)
        return 1
    finally:
        if forwarding is not None:
            forwarding.terminate()
            try:
                forwarding.wait(timeout=5)
            except subprocess.TimeoutExpired:
                forwarding.kill()
                forwarding.wait()


if __name__ == "__main__":
    raise SystemExit(main())
