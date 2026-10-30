"""Generate fake incident data for PagePal.

Each scenario gets a folder in data/scenarios/<name>/ with:
  alert.json    - the page that wakes up the on-call engineer
  logs.jsonl    - one JSON log line per row (same shape as Cloud Logging)
  metrics.json  - one data point per minute
  deploys.json  - recent deploy history

Run it again any time:  uv run python scripts/make_fake_data.py
The random seed is fixed, so the output is the same every time.
"""

import json
import random
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent / "data" / "scenarios"
START = datetime(2026, 10, 28, 13, 30, tzinfo=timezone.utc)
MINUTES = 60
INCIDENT_AT = 32  # minute where things go wrong

ENDPOINTS = ["/api/analyze", "/api/search", "/api/health", "/api/repo"]
MODELS = ["openai/gpt-oss-120b", "llama-3.3-70b-versatile"]


def ts(minute: int, second: int = 0) -> str:
    return (START + timedelta(minutes=minute, seconds=second)).isoformat()


def log(minute, second, service, severity, event, message, **extra):
    return {
        "timestamp": ts(minute, second),
        "severity": severity,
        "service": service,
        "event": event,
        "message": message,
        **extra,
    }


def normal_traffic(rng, service, minute, version):
    """A few healthy request logs, plus a harmless warning now and then."""
    rows = []
    for _ in range(rng.randint(2, 4)):
        path = rng.choice(ENDPOINTS)
        latency = rng.randint(40, 400)
        rows.append(log(minute, rng.randint(0, 59), service, "INFO", "request_finished",
                        f"GET {path} 200 in {latency}ms", path=path, status=200,
                        latency_ms=latency, version=version))
    if rng.random() < 0.3:
        tokens = rng.randint(300, 2500)
        rows.append(log(minute, rng.randint(0, 59), service, "INFO", "llm_call",
                        f"LLM call ok ({tokens} tokens)", model=MODELS[0], status=200,
                        tokens=tokens))
    # Red herring: this warning shows up in every scenario, before and after the incident.
    if rng.random() < 0.08:
        rows.append(log(minute, rng.randint(0, 59), service, "WARNING", "slow_query",
                        "Firestore query took 1200ms (collection=repos)", latency_ms=1200))
    return rows


def write(name, alert, logs, metrics, deploys):
    folder = ROOT / name
    folder.mkdir(parents=True, exist_ok=True)
    logs.sort(key=lambda r: r["timestamp"])
    (folder / "alert.json").write_text(json.dumps(alert, indent=2) + "\n")
    (folder / "logs.jsonl").write_text("".join(json.dumps(r) + "\n" for r in logs))
    (folder / "metrics.json").write_text(json.dumps(metrics, indent=2) + "\n")
    (folder / "deploys.json").write_text(json.dumps(deploys, indent=2) + "\n")
    print(f"wrote {folder}  ({len(logs)} log lines)")


def bad_deploy(rng):
    """A new version ships with a bug: /api/analyze starts returning 500s."""
    service = "codeatlas-api"
    logs, metrics = [], []
    for m in range(MINUTES):
        broken = m >= INCIDENT_AT
        version = "v1.8.0" if broken else "v1.7.3"
        logs += normal_traffic(rng, service, m, version)
        if broken:
            for _ in range(rng.randint(3, 6)):
                logs.append(log(m, rng.randint(0, 59), service, "ERROR", "request_finished",
                                "POST /api/analyze 500: KeyError: 'repo_url'",
                                path="/api/analyze", status=500, version=version,
                                error="KeyError: 'repo_url'"))
        metrics.append({
            "timestamp": ts(m),
            "error_rate_pct": round(rng.uniform(30, 45) if broken else rng.uniform(0.1, 0.8), 2),
            "p95_latency_ms": rng.randint(250, 420),
            "memory_mb": rng.randint(380, 450),
            "memory_limit_mb": 1024,
            "requests_per_min": rng.randint(80, 120),
        })
    logs.append(log(INCIDENT_AT, 0, service, "INFO", "deploy_finished",
                    "Revision codeatlas-api-00042 (v1.8.0) is serving 100% of traffic",
                    version="v1.8.0"))
    deploys = [
        {"service": service, "version": "v1.7.3", "finished_at": "2026-10-25T09:12:00+00:00"},
        {"service": service, "version": "v1.8.0", "finished_at": ts(INCIDENT_AT)},
    ]
    alert = {
        "id": "INC-1001",
        "title": "High 5xx error rate on codeatlas-api",
        "service": service,
        "severity": "SEV2",
        "fired_at": ts(INCIDENT_AT + 3),
        "details": "error_rate_pct > 5 for 3 minutes",
    }
    write("bad_deploy", alert, logs, metrics, deploys)


def memory_leak(rng):
    """Memory grows a bit every minute until the container is killed."""
    service = "codeatlas-worker"
    logs, metrics = [], []
    memory = 300.0
    for m in range(MINUTES):
        logs += normal_traffic(rng, service, m, "v1.7.3")
        memory += rng.uniform(25, 35)
        if memory >= 2048:
            logs.append(log(m, 5, service, "ERROR", "container_killed",
                            "Container exceeded memory limit (2048 MiB) and was restarted (OOMKilled)",
                            memory_mb=2048))
            memory = 300.0
        elif memory > 1700:
            logs.append(log(m, 30, service, "WARNING", "memory_high",
                            "Memory usage above 80% of the 2048 MiB limit", memory_mb=round(memory)))
        metrics.append({
            "timestamp": ts(m),
            "error_rate_pct": round(rng.uniform(0.1, 0.8), 2),
            "p95_latency_ms": rng.randint(250, 420) + int(memory / 4),
            "memory_mb": round(memory),
            "memory_limit_mb": 2048,
            "requests_per_min": rng.randint(30, 50),
        })
    deploys = [{"service": service, "version": "v1.7.3", "finished_at": "2026-10-25T09:12:00+00:00"}]
    alert = {
        "id": "INC-1002",
        "title": "codeatlas-worker restarted (OOMKilled)",
        "service": service,
        "severity": "SEV3",
        "fired_at": ts(MINUTES - 1),
        "details": "container restart count increased",
    }
    write("memory_leak", alert, logs, metrics, deploys)


def llm_rate_limit(rng):
    """The main Groq model hits its daily token limit. Every LLM call now fails with 429."""
    service = "codeatlas-api"
    logs, metrics = [], []
    for m in range(MINUTES):
        limited = m >= INCIDENT_AT
        logs += normal_traffic(rng, service, m, "v1.7.3")
        if limited:
            for _ in range(rng.randint(2, 4)):
                logs.append(log(m, rng.randint(0, 59), service, "ERROR", "llm_call",
                                "Groq 429 rate_limit_exceeded: tokens per day (TPD) limit reached",
                                model=MODELS[0], status=429, error="rate_limit_exceeded"))
                logs.append(log(m, rng.randint(0, 59), service, "ERROR", "request_finished",
                                "POST /api/analyze 503: upstream LLM unavailable",
                                path="/api/analyze", status=503, version="v1.7.3"))
        metrics.append({
            "timestamp": ts(m),
            "error_rate_pct": round(rng.uniform(20, 30) if limited else rng.uniform(0.1, 0.8), 2),
            "p95_latency_ms": rng.randint(250, 420),
            "memory_mb": rng.randint(380, 450),
            "memory_limit_mb": 1024,
            "requests_per_min": rng.randint(80, 120),
        })
    deploys = [{"service": service, "version": "v1.7.3", "finished_at": "2026-10-25T09:12:00+00:00"}]
    alert = {
        "id": "INC-1003",
        "title": "High 5xx error rate on codeatlas-api",
        "service": service,
        "severity": "SEV2",
        "fired_at": ts(INCIDENT_AT + 3),
        "details": "error_rate_pct > 5 for 3 minutes",
    }
    write("llm_rate_limit", alert, logs, metrics, deploys)


if __name__ == "__main__":
    rng = random.Random(42)
    bad_deploy(rng)
    memory_leak(rng)
    llm_rate_limit(rng)
