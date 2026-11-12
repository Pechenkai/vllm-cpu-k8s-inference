"""Замеры TTFT, задержки и пропускной способности без сохранения тел и токенов."""

import argparse
import base64
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import hashlib
import http.client
import json
import math
import os
from pathlib import Path
import platform
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid


class ProbeError(Exception):
    """В отчёты и консоль допускаются только фиксированные коды ошибок."""


@dataclass
class Sample:
    index: int
    success: bool
    ttft_s: float | None
    latency_s: float
    prompt_tokens: int = 0
    output_tokens: int = 0
    error: str | None = None


def percentile(values, quantile):
    """Линейная интерполяция отсортированных наблюдений, включая случай n=1."""
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * quantile
    low = math.floor(position)
    high = math.ceil(position)
    return ordered[low] + (ordered[high] - ordered[low]) * (position - low)


def events(response):
    """Чтение SSE-событий, включая комментарии и многострочные данные."""
    data = []
    for raw in response:
        line = raw.decode("utf-8").rstrip("\r\n")
        if line == "":
            if data:
                yield "\n".join(data)
                data = []
        elif line.startswith("data:"):
            data.append(line[5:].removeprefix(" "))
    if data:
        yield "\n".join(data)


def measure_stream(response, started, clock=time.perf_counter):
    first = None
    usage = None
    finished = False
    for data in events(response):
        if data == "[DONE]":
            elapsed = clock() - started
            if first is None or usage is None or not finished:
                raise ProbeError("incomplete_stream")
            if not isinstance(usage, dict):
                raise ProbeError("invalid_usage")
            counts = usage.get("prompt_tokens"), usage.get("completion_tokens")
            if any(type(value) is not int or value <= 0 for value in counts):
                raise ProbeError("invalid_usage")
            return first, elapsed, *counts
        try:
            event = json.loads(data)
            if not isinstance(event, dict):
                raise ProbeError("invalid_sse")
            if "error" in event:
                raise ProbeError("server_error")
            for choice in event.get("choices", []):
                content = choice.get("delta", {}).get("content")
                if content and first is None:
                    first = clock() - started
                finished |= choice.get("finish_reason") is not None
            if event.get("usage") is not None:
                usage = event["usage"]
        except (ValueError, TypeError, AttributeError):
            raise ProbeError("invalid_sse") from None
    raise ProbeError("missing_done")


def make_prompt(case, index, seed):
    # Уникальный префикс в начале ограничивает повторное использование кэша.
    nonce = hashlib.sha256(f"{seed}:{case}:{index}".encode()).hexdigest()[:24]
    records = "The record contains a name, a date, and a number. "
    return (
        f"Request {nonce}. "
        + records * (45 if case == "long" else 1)
        + "Summarize the records in English."
    )


def run_request(base_url, token, model, prompt, output_tokens, timeout, index):
    body = json.dumps(
        {
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "stream": True,
            "stream_options": {"include_usage": True},
            "temperature": 0,
            "max_tokens": output_tokens,
            # Расширение vLLM: фиксируем длину генерации даже при появлении EOS.
            "ignore_eos": True,
            "cache_salt": uuid.uuid4().hex,
        }
    ).encode()
    request = urllib.request.Request(
        base_url.rstrip("/") + "/v1/chat/completions",
        data=body,
        headers={
            "Authorization": "Bearer " + token,
            "Content-Type": "application/json",
        },
    )
    started = time.perf_counter()
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            ttft, latency, inputs, outputs = measure_stream(response, started)
        if outputs != output_tokens:
            raise ProbeError("output_length_mismatch")
        return Sample(index, True, ttft, latency, inputs, outputs)
    except urllib.error.HTTPError as error:
        code = f"http_{error.code}"
        error.close()
    except ProbeError as error:
        code = error.args[0]
    except (TimeoutError, socket.timeout):
        code = "timeout"
    except (OSError, ValueError, TypeError, http.client.HTTPException):
        code = "transport_or_protocol_error"
    return Sample(index, False, None, time.perf_counter() - started, error=code)


def summarize(samples, elapsed):
    successful = [sample for sample in samples if sample.success]
    ttft = [sample.ttft_s for sample in successful]
    latency = [sample.latency_s for sample in successful]
    return {
        "requests": len(samples),
        "successful": len(successful),
        "errors": len(samples) - len(successful),
        "elapsed_s": elapsed,
        "ttft_p50_s": percentile(ttft, 0.5),
        "ttft_p95_s": percentile(ttft, 0.95),
        "latency_p50_s": percentile(latency, 0.5),
        "latency_p95_s": percentile(latency, 0.95),
        "requests_per_s": len(successful) / elapsed,
        "output_tokens_per_s": sum(sample.output_tokens for sample in successful)
        / elapsed,
        "prompt_tokens_min": min((s.prompt_tokens for s in successful), default=None),
        "prompt_tokens_max": max((s.prompt_tokens for s in successful), default=None),
        "output_tokens_total": sum(s.output_tokens for s in successful),
    }


def run_case(base_url, token, args, case, concurrency):
    def probe(index):
        return run_request(
            base_url,
            token,
            args.model,
            make_prompt(case, index, f"{args.seed}:{concurrency}"),
            args.output_tokens,
            args.timeout,
            index,
        )

    # Отрицательные индексы не дают тестовым промптам повторять прогревочные.
    for index in range(-args.warmup, 0):
        if not probe(index).success:
            raise ProbeError("warmup_failed")
    started = time.perf_counter()
    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        samples = list(pool.map(probe, range(args.requests)))
    elapsed = time.perf_counter() - started
    return {
        "case": case,
        "concurrency": concurrency,
        "summary": summarize(samples, elapsed),
        "samples": [asdict(sample) for sample in samples],
    }


def kubectl_json(kubectl, *args):
    return json.loads(subprocess.check_output([*kubectl, *args, "-o", "json"]))


def cluster_snapshot(kubectl):
    pods = kubectl_json(kubectl, "get", "pods", "-l", "app=vllm")["items"]
    ready = [
        pod
        for pod in pods
        if not pod["metadata"].get("deletionTimestamp")
        and any(
            c["type"] == "Ready" and c["status"] == "True"
            for c in pod.get("status", {}).get("conditions", [])
        )
    ]
    if len(ready) != 1:
        raise ProbeError("expected_one_ready_pod")
    pod = ready[0]
    container = next(c for c in pod["spec"]["containers"] if c["name"] == "vllm")
    status = next(c for c in pod["status"]["containerStatuses"] if c["name"] == "vllm")
    # Явный выбор полей исключает случайную сериализацию значений Secret.
    config = {}
    for volume in pod["spec"]["volumes"]:
        if volume["name"] == "config":
            cm = kubectl_json(kubectl, "get", "configmap", volume["configMap"]["name"])
            config["serve.yaml"] = cm["data"]["serve.yaml"]
    for reference in container.get("envFrom", []):
        if "configMapRef" in reference:
            cm = kubectl_json(
                kubectl, "get", "configmap", reference["configMapRef"]["name"]
            )
            config["environment"] = {
                key: value
                for key, value in cm["data"].items()
                if key
                in {
                    "MODEL_ID",
                    "MODEL_REVISION",
                    "OMP_NUM_THREADS",
                    "VLLM_CPU_OMP_THREADS_BIND",
                    "VLLM_CPU_KVCACHE_SPACE",
                }
            }
    return {
        "pod_uid": pod["metadata"]["uid"],
        "image": container["image"],
        "image_id": status["imageID"],
        "restart_count": status["restartCount"],
        "resources": container["resources"],
        "config": config,
    }


@contextmanager
def endpoint(args):
    if args.base_url:
        token = os.environ.get("VLLM_API_KEY")
        if not token:
            raise ProbeError("missing_api_key")
        yield args.base_url, token, None
        return
    kubectl = ["kubectl", "--context", args.context, "-n", args.namespace]
    secret = kubectl_json(kubectl, "get", "secret", "vllm-api")
    token = base64.b64decode(secret["data"]["api-key"], validate=True).decode()
    if not token:
        raise ProbeError("empty_api_key")
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
    try:
        for _ in range(60):
            if forwarding.poll() is not None:
                raise ProbeError("port_forward_exited")
            try:
                with urllib.request.urlopen(base_url + "/health", timeout=2):
                    break
            except OSError:
                time.sleep(0.5)
        else:
            raise ProbeError("health_timeout")
        yield base_url, token, kubectl
    finally:
        forwarding.terminate()
        try:
            forwarding.wait(timeout=5)
        except subprocess.TimeoutExpired:
            forwarding.kill()
            forwarding.wait()


def host_info():
    cpu = next(
        (
            line.split(":", 1)[1].strip()
            for line in Path("/proc/cpuinfo").read_text().splitlines()
            if line.startswith("model name")
        ),
        "unknown",
    )
    memory = next(
        line.split()[1]
        for line in Path("/proc/meminfo").read_text().splitlines()
        if line.startswith("MemTotal:")
    )
    return {
        "cpu": cpu,
        "logical_cpus": os.cpu_count(),
        "ram_kib": int(memory),
        "architecture": platform.machine(),
        "client_python": platform.python_version(),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--context", default="kaspersky")
    parser.add_argument("--namespace", default="kaspersky-llm")
    parser.add_argument(
        "--base-url", help="Direct endpoint; reads VLLM_API_KEY from environment"
    )
    parser.add_argument("--model", default="Qwen/Qwen2.5-0.5B-Instruct")
    parser.add_argument(
        "--requests",
        type=int,
        default=12,
        help="Measured requests per case/concurrency",
    )
    parser.add_argument(
        "--warmup", type=int, default=1, help="Unmeasured requests before each case"
    )
    parser.add_argument("--concurrency", type=int, nargs="+", default=[1, 2])
    parser.add_argument("--output-tokens", type=int, default=32)
    parser.add_argument(
        "--timeout", type=float, default=180, help="Socket I/O timeout in seconds"
    )
    parser.add_argument("--seed", type=int, default=20261110)
    parser.add_argument(
        "--output", type=Path, default=Path("bench/results/latest.json")
    )
    args = parser.parse_args()
    if (
        args.requests < 1
        or args.warmup < 0
        or args.output_tokens < 1
        or not math.isfinite(args.timeout)
        or args.timeout <= 0
        or any(c < 1 or c > args.requests for c in args.concurrency)
    ):
        parser.error(
            "Positive counts/timeout required; concurrency must be <= requests"
        )
    try:
        report = {
            "schema": 1,
            "started_at": datetime.now(timezone.utc).isoformat(),
            "host": host_info(),
            "model": args.model,
            "settings": {
                key: getattr(args, key)
                for key in (
                    "requests",
                    "warmup",
                    "concurrency",
                    "output_tokens",
                    "timeout",
                    "seed",
                )
            },
            "transport": "direct" if args.base_url else "kubectl_port_forward",
            "cases": [],
        }
        with endpoint(args) as (base_url, token, kubectl):
            before = cluster_snapshot(kubectl) if kubectl else None
            report["server_before"] = before
            for case in ("short", "long"):
                for concurrency in args.concurrency:
                    print(f"Running {case}, concurrency={concurrency}", flush=True)
                    result = run_case(base_url, token, args, case, concurrency)
                    report["cases"].append(result)
                    summary = result["summary"]
                    print(
                        f"Completed: {summary['successful']}/{summary['requests']}; "
                        f"{summary['output_tokens_per_s']:.2f} output tokens/s",
                        flush=True,
                    )
            after = cluster_snapshot(kubectl) if kubectl else None
            report["server_after"] = after
            report["server_unchanged"] = before == after if kubectl else None
        report["finished_at"] = datetime.now(timezone.utc).isoformat()
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n")
        print("Benchmark saved; request bodies, responses and credentials excluded")
        return int(
            report["server_unchanged"] is False
            or any(c["summary"]["errors"] for c in report["cases"])
        )
    except (
        ProbeError,
        OSError,
        ValueError,
        KeyError,
        subprocess.CalledProcessError,
    ) as error:
        # Исключения HTTP и подпроцессов могут содержать приватные данные запроса.
        code = error.args[0] if isinstance(error, ProbeError) else type(error).__name__
        print(f"Benchmark failed: {code}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
