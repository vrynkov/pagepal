"""Tools the investigator agent can call.

Week 1: they read fake data from data/scenarios/<scenario>/.
Later we swap the insides for real Cloud Logging / Docker calls,
and the agent does not need to change.

Groq's free tier allows only ~8K tokens per minute, so every tool returns
a SHORT summary, never the whole log file.
"""

import json
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from langchain.tools import ToolRuntime, tool

DATA_DIR = Path(__file__).resolve().parents[2] / "data" / "scenarios"


@dataclass
class IncidentContext:
    """Passed to every tool at run time. The LLM never sees or changes it."""

    scenario: str

    @property
    def folder(self) -> Path:
        return DATA_DIR / self.scenario


def list_scenarios() -> list[str]:
    return sorted(p.name for p in DATA_DIR.iterdir() if (p / "alert.json").exists())


def load_alert(scenario: str) -> dict:
    return json.loads((DATA_DIR / scenario / "alert.json").read_text())


def _read_logs(folder: Path) -> list[dict]:
    with open(folder / "logs.jsonl") as f:
        return [json.loads(line) for line in f]


@tool
def get_error_summary(runtime: ToolRuntime[IncidentContext]) -> str:
    """Count ERROR and WARNING logs, grouped by message, with first and last time seen.
    Start here: it shows what is failing and since when."""
    groups: Counter = Counter()
    first_seen: dict = {}
    last_seen: dict = {}
    for row in _read_logs(runtime.context.folder):
        if row["severity"] not in ("ERROR", "WARNING"):
            continue
        key = (row["severity"], row["event"], row["message"])
        groups[key] += 1
        first_seen.setdefault(key, row["timestamp"])
        last_seen[key] = row["timestamp"]
    if not groups:
        return "No ERROR or WARNING logs found."
    lines = []
    for (sev, event, msg), count in groups.most_common(10):
        lines.append(f"{count}x {sev} [{event}] {msg} | first {first_seen[(sev, event, msg)]} "
                     f"| last {last_seen[(sev, event, msg)]}")
    return "\n".join(lines)


@tool
def search_logs(text: str, runtime: ToolRuntime[IncidentContext], limit: int = 10) -> str:
    """Return up to `limit` log lines whose message contains `text` (case insensitive).
    Use it to look at examples of a specific error or event."""
    limit = max(1, min(limit, 20))
    hits = [row for row in _read_logs(runtime.context.folder)
            if text.lower() in row["message"].lower() or text.lower() == row["event"]]
    if not hits:
        return f"No log lines contain '{text}'."
    shown = [json.dumps(row) for row in hits[:limit]]
    return f"{len(hits)} matching lines, showing {len(shown)}:\n" + "\n".join(shown)


@tool
def get_metrics(runtime: ToolRuntime[IncidentContext]) -> str:
    """Show service metrics (error rate, latency, memory, traffic) as averages per 10 minutes.
    Use it to see when a problem started and how big it is."""
    points = json.loads((runtime.context.folder / "metrics.json").read_text())
    lines = ["window_start | error_rate_pct | p95_latency_ms | memory_mb (max/limit) | requests_per_min"]
    for i in range(0, len(points), 10):
        chunk = points[i:i + 10]

        def avg(key):
            return round(sum(p[key] for p in chunk) / len(chunk), 1)

        max_mem = max(p["memory_mb"] for p in chunk)
        lines.append(f"{chunk[0]['timestamp']} | {avg('error_rate_pct')} | {avg('p95_latency_ms')} | "
                     f"{max_mem}/{chunk[0]['memory_limit_mb']} | {avg('requests_per_min')}")
    return "\n".join(lines)


@tool
def get_recent_deploys(runtime: ToolRuntime[IncidentContext]) -> str:
    """List recent deploys (service, version, finish time). A deploy right before
    an incident is a common cause."""
    deploys = json.loads((runtime.context.folder / "deploys.json").read_text())
    return "\n".join(f"{d['finished_at']} {d['service']} {d['version']}" for d in deploys)


INVESTIGATION_TOOLS = [get_error_summary, search_logs, get_metrics, get_recent_deploys]
