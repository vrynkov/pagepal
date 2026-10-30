"""PagePal main flow, built with LangGraph.

    receive_alert -> investigate -> propose_fix -> human_approval -> act
                                         |               |
                                         |               +-- rejected --> END
                                         +-- escalate_to_human --> END

`graph` at the bottom is what `langgraph dev` loads (see langgraph.json).
"""

from typing import Literal, TypedDict

from langchain.agents import create_agent
from langchain.agents.middleware import ModelFallbackMiddleware
from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt
from pydantic import BaseModel, Field

from pagepal.llm import make_models
from pagepal.tools import INVESTIGATION_TOOLS, IncidentContext, load_alert

# ---------------------------------------------------------------- state


class IncidentState(TypedDict, total=False):
    """Everything the graph knows about one incident. Each node fills in a part."""

    scenario: str          # input: which fake incident to load, e.g. "bad_deploy"
    alert: dict            # the page that fired
    findings: str          # what the investigator found
    proposal: dict         # the fix the LLM suggests (a FixProposal as a dict)
    approved: bool         # the human's answer
    reviewer_note: str     # optional comment from the human
    result: str            # what happened in the end


Action = Literal[
    "rollback_deploy",
    "restart_service",
    "switch_llm_model",
    "scale_up_memory",
    "escalate_to_human",
]


class FixProposal(BaseModel):
    """The LLM must answer in exactly this shape. `action` can only be one of the
    allowed values, so the LLM cannot invent a dangerous command."""

    root_cause: str = Field(description="One or two sentences: what is broken and why.")
    action: Action = Field(description="The single best fix. Use escalate_to_human if unsure.")
    target: str = Field(description="The service to apply the fix to, e.g. codeatlas-api.")
    explanation: str = Field(description="Why this fix, citing evidence from the findings.")
    confidence: Literal["low", "medium", "high"]


# ---------------------------------------------------------------- LLM pieces

INVESTIGATOR_PROMPT = """You are PagePal, an on-call SRE assistant.
An alert fired. Use the tools to find the root cause. Be efficient: usually
3-5 tool calls are enough. Check errors, metrics and recent deploys.
Ignore noise that was also there before the incident started.
Finish with a short report: timeline, evidence (quote log lines or numbers),
and the most likely root cause."""

PROPOSER_PROMPT = """You are PagePal, an on-call SRE assistant.
Based on the alert and the investigation findings, propose ONE fix.
Allowed actions:
- rollback_deploy: a recent deploy broke the service
- restart_service: the service is in a bad state (e.g. memory leak) and a restart buys time
- switch_llm_model: an LLM provider/model is failing or rate limited
- scale_up_memory: the service needs more memory under normal load
- escalate_to_human: evidence is unclear or nothing above fits"""


def make_investigator():
    """A small ReAct agent: the LLM calls tools in a loop until it is done."""
    first, *backups = make_models()
    middleware = [ModelFallbackMiddleware(*backups)] if backups else []
    return create_agent(
        model=first,
        tools=INVESTIGATION_TOOLS,
        system_prompt=INVESTIGATOR_PROMPT,
        middleware=middleware,
        context_schema=IncidentContext,
    )


def make_fix_proposer():
    """An LLM that returns a FixProposal object instead of free text."""
    first, *backups = [m.with_structured_output(FixProposal) for m in make_models()]
    return first.with_fallbacks(backups) if backups else first


# ---------------------------------------------------------------- nodes


def receive_alert(state: IncidentState) -> IncidentState:
    return {"alert": load_alert(state["scenario"])}


def investigate(state: IncidentState) -> IncidentState:
    alert = state["alert"]
    question = f"Alert: {alert}\nInvestigate this incident."
    out = make_investigator().invoke(
        {"messages": [{"role": "user", "content": question}]},
        context=IncidentContext(scenario=state["scenario"]),
        config={"recursion_limit": 20},  # stop runaway tool loops
    )
    return {"findings": out["messages"][-1].content}


def propose_fix(state: IncidentState) -> IncidentState:
    proposal = make_fix_proposer().invoke([
        {"role": "system", "content": PROPOSER_PROMPT},
        {"role": "user", "content": f"Alert: {state['alert']}\n\nFindings:\n{state['findings']}"},
    ])
    return {"proposal": proposal.model_dump()}


def human_approval(state: IncidentState) -> IncidentState:
    """Pause the graph and wait for a human. The graph resumes with
    Command(resume=...) where the value is True/False, "yes"/"no",
    or {"approved": bool, "note": str}."""
    answer = interrupt({
        "question": "Approve this fix?",
        "alert": state["alert"]["title"],
        "proposal": state["proposal"],
    })
    note = ""
    if isinstance(answer, dict):
        note = answer.get("note", "")
        answer = answer.get("approved", False)
    if isinstance(answer, str):
        answer = answer.strip().lower() in ("y", "yes", "approve", "approved", "true")
    return {"approved": bool(answer), "reviewer_note": note}


# Week 1: actions are simulated. Later they will call real APIs (Cloud Run, Docker).
RUNBOOK = {
    "rollback_deploy": "Rolled back {target} to the previous version",
    "restart_service": "Restarted {target}",
    "switch_llm_model": "Switched {target} to the next fallback LLM model",
    "scale_up_memory": "Raised memory limit of {target} by 50%",
}


def act(state: IncidentState) -> IncidentState:
    proposal = state["proposal"]
    service = state["alert"]["service"]
    # Safety check: never touch a service other than the one that alerted.
    if proposal["target"] != service:
        return {"result": f"Blocked: proposal targets {proposal['target']}, alert is for {service}."}
    done = RUNBOOK[proposal["action"]].format(target=service)
    return {"result": f"[SIMULATED] {done}."}


def escalate(state: IncidentState) -> IncidentState:
    return {"result": "Escalated to a human: PagePal was not sure what to do."}


def rejected(state: IncidentState) -> IncidentState:
    note = state.get("reviewer_note") or "no note"
    return {"result": f"Fix rejected by human ({note}). Nothing was changed."}


# ---------------------------------------------------------------- routing


def after_proposal(state: IncidentState) -> str:
    return "escalate" if state["proposal"]["action"] == "escalate_to_human" else "human_approval"


def after_approval(state: IncidentState) -> str:
    return "act" if state["approved"] else "rejected"


# ---------------------------------------------------------------- graph


def build_graph(checkpointer=None):
    """Interrupts need a checkpointer (it saves the paused state).
    `langgraph dev` adds its own, so we only pass one when running from Python."""
    g = StateGraph(IncidentState)
    g.add_node(receive_alert)
    g.add_node(investigate)
    g.add_node(propose_fix)
    g.add_node(human_approval)
    g.add_node(act)
    g.add_node(escalate)
    g.add_node(rejected)

    g.add_edge(START, "receive_alert")
    g.add_edge("receive_alert", "investigate")
    g.add_edge("investigate", "propose_fix")
    g.add_conditional_edges("propose_fix", after_proposal, ["human_approval", "escalate"])
    g.add_conditional_edges("human_approval", after_approval, ["act", "rejected"])
    g.add_edge("act", END)
    g.add_edge("escalate", END)
    g.add_edge("rejected", END)
    return g.compile(checkpointer=checkpointer, name="pagepal")


graph = build_graph()
