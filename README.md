# CPU-инференс LLM в Kubernetes - тестовое задание Kaspersky

**Пантелеев В. М. Команда исследования технологий машинного обучения**

Развёртывание `Qwen/Qwen2.5-0.5B-Instruct` через **vLLM 0.27.0 на CPU** в Minikube. 
Два собственных rootless-образа (vLLM и загрузчик), 
закреплённые Python-зависимости, постоянный кэш весов в PVC, 
авторизация Bearer-токеном, приватное логирование. 
Реализованы все три бонуса: **Kustomize, initContainer, 
нагрузочное тестирование**.

## 1. Структура

| Путь | Назначение |
|---|---|
| `Dockerfile`, `requirements.txt` | CPU-образ vLLM, UID/GID `10001`, lock транзитивных зависимостей с SHA-256 |
| `app/`, `sitecustomize.py` | Защищённый запуск, JSON-аудит без содержимого запросов и ответов |
| `downloader/` | rootless initContainer, отдельный Dockerfile, загрузка модели |
| `k8s/` | Deployment, Service, PVC, Secret-пример, ConfigMap, Kustomize, offline overlay |
| `scripts/`, `tests/` | Создание Secret, smoke/privacy-проверки, unit-тесты |
| `bench/` | Streaming-бенчмарк и JSON с результатами |

Модель закреплена за ревизией `7ae557604adf67be50417f59c2c2f167def9a775`. 
При первом запуске initContainer скачивает snapshot Hugging Face на PVC; 
при следующих проверяет локальный manifest и SHA-256, сообщает `cache_hit` и не обращается к HF. 
Основной контейнер читает PVC только для чтения с `HF_HUB_OFFLINE=1`. 


## 2. Запуск в Minikube

Проверенная среда: **Linux x86_64**, Docker, Minikube **1.39.0**, Kubernetes/kubectl **1.35.10**, Calico; 
кластеру выделены **6 CPU / 8 GiB RAM**.
Команды выполняются из корня репозитория.

```bash
# Собрать и импортировать образы
docker build -t kaspersky-vllm-cpu:0.27.0 .
docker build -t kaspersky-model-downloader:1 downloader
minikube start --profile kaspersky --driver=docker \
  --container-runtime=containerd --kubernetes-version=v1.35.10 \
  --cpus=6 --memory=8192 --cni=calico
minikube -p kaspersky image load --daemon=true kaspersky-vllm-cpu:0.27.0
minikube -p kaspersky image load --daemon=true kaspersky-model-downloader:1

# Создать namespace и Secret, затем развернуть приложение
kubectl --context kaspersky apply -f k8s/base/namespace.yaml
python3 scripts/create_k8s_secrets.py --context kaspersky
kubectl --context kaspersky apply -k k8s
kubectl --context kaspersky -n kaspersky-llm rollout status deployment/vllm --timeout=15m
python3 scripts/smoke_k8s.py --context kaspersky --privacy
```

`create_k8s_secrets.py` читает `VLLM_API_KEY` из окружения либо генерирует токен и создаёт Kubernetes Secret 
`vllm-api` без записи ключа в репозиторий или аргументы процесса. 
Для приватной заранее необходимо задать `HF_TOKEN` в окружении:
скрипт создаст отдельный Secret `huggingface-token`, 
доступный только initContainer. 
Выбранная публичная Qwen не требует HF-токена. 

Проверить состояние: 

```bash
kubectl --context kaspersky -n kaspersky-llm get pods,pvc
kubectl --context kaspersky -n kaspersky-llm logs deployment/vllm -c download-model
kubectl --context kaspersky -n kaspersky-llm logs deployment/vllm -c vllm --tail=20
```

### Запрос к API

В отдельном терминале запустить port-forward:

```bash
kubectl --context kaspersky -n kaspersky-llm port-forward service/vllm 8001:8000
```

В клиентском терминале:

```bash
export VLLM_API_KEY="$(kubectl --context kaspersky -n kaspersky-llm \
  get secret vllm-api -o jsonpath='{.data.api-key}' | base64 --decode)"
printf 'Authorization: Bearer %s\n' "$VLLM_API_KEY" \
  | curl --fail-with-body http://127.0.0.1:8001/v1/chat/completions \
    --header @- -H 'Content-Type: application/json' \
    -d '{"model":"Qwen/Qwen2.5-0.5B-Instruct","messages":[{"role":"user","content":"Привет!"}],"max_tokens":16}'
```

Без корректного Bearer-токена API инференса возвращает 
`401`. `/health` открыт для Kubernetes probes.

## 3. Конфигурация и безопасность

- **ConfigMap:** `k8s/base/model.env` - ID/ревизия модели, 
CPU-потоки и KV-cache; `k8s/base/serve.yaml` - параметры vLLM 
(`max-model-len`, `max-num-seqs`, batching, dtype...). 
- **Rootless:** оба контейнера работают с UID/GID `10001`, 
без повышения привилегий и Linux capabilities; 
read-only root filesystem и отдельные writable mounts. 
ServiceAccount token не монтируется. Основной контейнер не получает HF-токен.
- **Приватность:** сервер стартует только через `python -m app.serve`; 
код из `app/` и `sitecustomize.py` блокирует вывод промптов, ответов, 
заголовков и текста исключений в stdout/stderr. 
Аудит сохраняет ID запроса, операцию, HTTP-статус, 
длительность и результат без пользовательского содержимого. 
- **Зависимости:** `requirements.txt` сервера и загрузчика включают закреплённые 
прямые и транзитивные пакеты с хешами. `requirements.in` - исходные спецификации. 

**Проверка повторного запуска без сети** (выполнять только после успешной первой загрузки):

```bash
kubectl --context kaspersky apply -k k8s/overlays/offline
kubectl --context kaspersky -n kaspersky-llm rollout restart deployment/vllm
kubectl --context kaspersky -n kaspersky-llm rollout status deployment/vllm --timeout=15m
kubectl --context kaspersky -n kaspersky-llm logs deployment/vllm -c download-model
python3 scripts/smoke_k8s.py --context kaspersky --privacy
```

## 4. Нагрузочное тестирование — бонус 3

```bash
python3 bench/benchmark.py --context kaspersky \
  --requests 12 --warmup 1 --concurrency 1 2 --output-tokens 32 \
  --output bench/results/my-run.json
```

Клиент работает на стандартной библиотеке Python 3.12, сам открывает временный port-forward 
и читает Secret. Использует streaming `/v1/chat/completions`. 
**TTFT** — время до первой непустой `delta.content`; 
**latency** — время до `[DONE]`; 
**throughput** — успешные запросы или выходные токены, делённые на wall time серии (без прогрева).
`p50/p95` рассчитаны по успешным запросам.

**Результат:** 48 запросов, 32 выходных токена на запрос. Сервер: 
`float32`, `max-model-len=1024`, `max-num-seqs=1`, `max-num-batched-tokens=1024`, 
KV-cache 1 GiB, 4 CPU / 7 GiB RAM. 
Четыре сценария — размер входа × число параллельных клиентов:

| Вход | Клиенты | Токены входа | TTFT p50/p95, с | Latency p50/p95, с | Запр./с | Выходных токенов/с |
|---|---:|---:|---:|---:|---:|---:|
| Короткий | 1 | 70–77 | 0.489 / 0.516 | 4.198 / 4.229 | 0.238 | 7.62 |
| Короткий | 2 | 72–77 | 4.708 / 4.888 | 8.429 / 8.680 | 0.236 | 7.54 |
| Длинный | 1 | 644–648 | 2.061 / 2.075 | 5.784 / 5.840 | 0.173 | 5.53 |
| Длинный | 2 | 642–647 | 7.834 / 7.994 | 11.551 / 11.770 | 0.172 | 5.51 |

Исходные результаты: [`bench/results/2026-11-12-acceptance.json`](bench/results/2026-11-12-acceptance.json). 
При двух клиентах пропускная способность практически не выросла: сервер допускает только 
одну одновременную последовательность.

**Оптимизация throughput (гипотезы, не измеренные ускорения):**

- **А. Много коротких запросов:** повысить `max-num-seqs` с 1 до 2–4, подобрать `max-num-batched-tokens`, сравнить OMP-потоки и распределение CPU. Измерять throughput вместе с p95 latency.
- **Б. Мало запросов с большим контекстом:** увеличить `max-model-len` и `VLLM_CPU_KVCACHE_SPACE` в пределах доступной RAM, подобрать prefill-бюджет (`max-num-batched-tokens`), проверить поддержку chunked prefill CPU backend. Увеличение лимитов само по себе не ускоряет инференс.

## 5. Проверки и ограничения

```bash
python3 -m unittest discover -s tests -v
python3 scripts/smoke_k8s.py --context kaspersky --privacy
```
Для остановки без удаления PVC: `minikube stop --profile kaspersky`. 
Удаление PVC, namespace или профиля Minikube может удалить веса.
