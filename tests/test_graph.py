"""Offline tests: no Groq key and no internet needed.

We replace the real LLMs with fakes that return scripted answers,
so we test the wiring of the graph, the tools and the human approval step.
"""

import pytest
from langchain_core.language_models import GenericFakeChatModel
from langchain_core.messages import AIMessage
from langchain_core.runnables import RunnableLambda
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

from pagepal import graph as pagepal_graph
from pagepal.graph import FixProposal, build_graph
from pagepal.tools import INVESTIGATION_TOOLS, IncidentContext, list_scenarios


class FakeToolModel(GenericFakeChatModel):
    """A fake chat model that 'supports' tools: it just replays scripted messages."""

    def bind_tools(self, tools, **kwargs):
        return self


def fake_investigator():
    from langchain.agents import create_agent

    script = iter([
        AIMessage(content="", tool_calls=[
            {"name": "get_error_summary", "args": {}, "id": "call_1"},
            {"name": "get_recent_deploys", "args": {}, "id": "call_2"},
        ]),
        AIMessage(content="Errors started right after deploy v1.8.0."),
    ])
    return create_agent(model=FakeToolModel(messages=script), tools=INVESTIGATION_TOOLS,
                        context_schema=IncidentContext)


def fake_proposer(action="rollback_deploy", target="codeatlas-api"):
    proposal = FixProposal(root_cause="Bad deploy v1.8.0", action=action, target=target,
                           explanation="500s began at the deploy time", confidence="high")
    return lambda: RunnableLambda(lambda _: proposal)


@pytest.fixture
def run(monkeypatch):
    def _run(scenario="bad_deploy", resume=None, **proposal):
        monkeypatch.setattr(pagepal_graph, "make_investigator", fake_investigator)
        monkeypatch.setattr(pagepal_graph, "make_fix_proposer", fake_proposer(**proposal))
        g = build_graph(checkpointer=InMemorySaver())
        config = {"configurable": {"thread_id": "test"}}
        state = g.invoke({"scenario": scenario}, config)
        if resume is not None:
            state = g.invoke(Command(resume=resume), config)
        return state
    return _run


def test_scenarios_exist():
    assert list_scenarios() == ["bad_deploy", "llm_rate_limit", "memory_leak"]


def test_pauses_for_human_approval(run):
    state = run()
    assert "__interrupt__" in state
    assert "deploy v1.8.0" in state["findings"]
    assert "result" not in state  # nothing happened yet


def test_approved_fix_is_applied(run):
    state = run(resume=True)
    assert state["result"] == "[SIMULATED] Rolled back codeatlas-api to the previous version."


def test_rejected_fix_does_nothing(run):
    state = run(resume={"approved": "no", "note": "too risky"})
    assert "rejected" in state["result"] and "too risky" in state["result"]


def test_escalation_skips_approval(run):
    state = run(action="escalate_to_human")
    assert "__interrupt__" not in state
    assert state["result"].startswith("Escalated")


def test_wrong_target_is_blocked(run):
    state = run(resume=True, target="some-other-service")
    assert state["result"].startswith("Blocked")


@pytest.mark.parametrize("scenario, expected", [
    ("bad_deploy", "KeyError: 'repo_url'"),
    ("memory_leak", "OOMKilled"),
    ("llm_rate_limit", "rate_limit_exceeded"),
])
def test_error_summary_finds_the_failure(scenario, expected):
    from pagepal.tools import get_error_summary
    from langgraph.prebuilt.tool_node import ToolRuntime

    runtime = ToolRuntime(state={}, context=IncidentContext(scenario), config={},
                          stream_writer=None, tool_call_id="t", store=None)
    out = get_error_summary.func(runtime=runtime)
    assert expected in out
    assert len(out) < 2000  # must stay small for Groq's free token limits
