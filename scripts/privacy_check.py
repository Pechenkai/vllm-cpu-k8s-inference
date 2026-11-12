"""Проверка приватности по объединённым логам Pod без вывода их содержимого."""

import json
import time
import urllib.error
import urllib.request
import uuid


class PrivacyCheckError(RuntimeError):
    """Фиксированное диагностическое сообщение без приватных значений."""


def inspect_logs(logs, markers, answers):
    def present(values, text):
        return any(
            variant in text
            for value in values
            for variant in (
                value,
                json.dumps(value, ensure_ascii=True)[1:-1],
                json.dumps(value, ensure_ascii=False)[1:-1],
            )
        )

    if present(markers, logs):
        raise PrivacyCheckError("Sensitive marker or credential found in Pod logs")
    records = []
    normalized = []
    common = {"time", "level", "pid", "event"}
    fields = {
        "diagnostic": {"logger", "line", "exception"},
        "request_finished": {
            "request_id",
            "operation",
            "status",
            "outcome",
            "duration_ms",
        },
    }
    public = {
        "level": {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"},
        "event": set(fields),
        "operation": {"chat_completions", "completions", "embeddings", "other"},
        "outcome": {"complete", "incomplete", "error", "cancelled"},
    }
    for line in logs.splitlines():
        try:
            record = json.loads(line)
        except ValueError:
            raise PrivacyCheckError("Unstructured output found in Pod logs") from None
        if (
            not isinstance(record, dict)
            or not isinstance(record.get("event"), str)
            or record.get("event") not in fields
            or set(record) != common | fields[record["event"]]
        ):
            raise PrivacyCheckError("Unexpected fields found in Pod logs")
        records.append(record)
        safe = dict(record)
        for key, allowed in public.items():
            if key in safe:
                if not isinstance(safe[key], str) or safe[key] not in allowed:
                    raise PrivacyCheckError("Unexpected metadata value in Pod logs")
                # Сгенерированное слово, например 'complete', может совпасть
                # с публичным значением аудита; само такое совпадение не означает утечку.
                safe[key] = ""
        # Имена полей также являются публичными фиксированными метаданными схемы.
        normalized.append(json.dumps(list(safe.values()), ensure_ascii=True))
    if present(answers, "\n".join(normalized)):
        raise PrivacyCheckError("Generated response found in Pod logs")
    return records


def check_privacy(base_url, token, model, read_logs):
    marker = "PRIVATE_PROBE_" + uuid.uuid4().hex
    markers = {marker, token}
    answers = set()
    audit_ids = set()

    def request(payload, bearer=token):
        return urllib.request.urlopen(
            urllib.request.Request(
                base_url + "/v1/chat/completions?probe=" + marker,
                data=json.dumps(payload).encode(),
                headers={
                    "Content-Type": "application/json",
                    "Authorization": "Bearer " + bearer,
                    "X-Request-Id": marker,
                },
            ),
            timeout=180,
        )

    def remember(response):
        identifier = response.headers.get("X-Audit-Id")
        if not identifier:
            raise PrivacyCheckError("Response lacks server-generated audit ID")
        audit_ids.add(identifier)

    payload = {
        "model": model,
        "messages": [{"role": "user", "content": "Repeat this string: " + marker}],
        "max_tokens": 16,
    }
    with request(payload) as response:
        remember(response)
        result = json.load(response)
        answer = result["choices"][0]["message"]["content"]
        if not answer:
            raise PrivacyCheckError("Privacy probe produced no text")
        if len(answer.strip()) >= 8:
            answers.add(answer.strip())

    for changes, bearer, expected in (
        ({"max_tokens": marker}, token, 400),
        ({}, marker, 401),
    ):
        try:
            with request({**payload, **changes}, bearer):
                raise PrivacyCheckError("Invalid privacy probe unexpectedly succeeded")
        except urllib.error.HTTPError as response:
            if response.code != expected:
                raise PrivacyCheckError("Unexpected privacy probe status") from None
            remember(response)
            response.close()

    for interrupt in (False, True):
        text = ""
        done = False
        with request({**payload, "stream": True, "max_tokens": 64}) as response:
            remember(response)
            for raw in response:
                line = raw.decode().strip()
                if not line.startswith("data: "):
                    continue
                if line[6:] == "[DONE]":
                    done = True
                    break
                for choice in json.loads(line[6:]).get("choices", []):
                    delta = choice.get("delta", {}).get("content") or ""
                    text += delta
                    if len(delta.strip()) >= 8:
                        answers.add(delta.strip())
                if interrupt and text:
                    break
        if not text or (not interrupt and not done):
            raise PrivacyCheckError(
                "Privacy streaming probe produced no complete output"
            )
        if len(text.strip()) >= 8:
            answers.add(text.strip())

    for _ in range(30):
        logs = read_logs()
        records = [
            record
            for record in inspect_logs(logs, markers, answers)
            if record["event"] == "request_finished"
        ]
        matched = {record["request_id"] for record in records} & audit_ids
        if matched == audit_ids:
            for identifier in audit_ids:
                if sum(r["request_id"] == identifier for r in records) != 1:
                    raise PrivacyCheckError("Expected exactly one audit per probe")
            print(
                "Privacy: 5 probes audited; prompt, response and credentials absent from Pod logs"
            )
            return
        time.sleep(1)
    raise PrivacyCheckError("Missing completion audit for privacy probe")
