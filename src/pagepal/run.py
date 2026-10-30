"""Run PagePal from the terminal.

    uv run pagepal                 # list scenarios
    uv run pagepal bad_deploy      # run one incident, you approve or reject the fix
"""

import sys
import uuid

from dotenv import load_dotenv
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

from pagepal.graph import build_graph
from pagepal.tools import list_scenarios


def main() -> None:
    load_dotenv()
    scenarios = list_scenarios()
    if len(sys.argv) < 2 or sys.argv[1] not in scenarios:
        print("Usage: uv run pagepal <scenario>")
        print("Scenarios:", ", ".join(scenarios))
        sys.exit(1)

    graph = build_graph(checkpointer=InMemorySaver())
    # The thread_id is how the checkpointer finds this run again after the pause.
    config = {"configurable": {"thread_id": str(uuid.uuid4())}}

    state = graph.invoke({"scenario": sys.argv[1]}, config)
    print(f"\n=== ALERT ===\n{state['alert']['title']}")
    print(f"\n=== FINDINGS ===\n{state['findings']}")

    if "__interrupt__" in state:  # the graph paused at human_approval
        p = state["proposal"]
        print("\n=== PROPOSED FIX ===")
        print(f"Root cause : {p['root_cause']}")
        print(f"Action     : {p['action']} on {p['target']} (confidence: {p['confidence']})")
        print(f"Why        : {p['explanation']}")
        answer = input("\nApprove? [y/N] ")
        note = "" if answer.lower().startswith("y") else input("Why not? (optional) ")
        state = graph.invoke(Command(resume={"approved": answer, "note": note}), config)

    print(f"\n=== RESULT ===\n{state['result']}")


if __name__ == "__main__":
    main()
