"""Assembles the decisioning graph.

    case_assembly -> scoring -> prioritise -+-> strategy -> draft -> guard -> route -> END
                                            |                        ^        ^
                                            +-- screening not clear -+        |
                                            +-- divert / not worth contacting-+
    case_assembly -> route            (entity has no delinquent account)

The screening short-circuit skips strategy and draft (two paid model calls) for cases the guard
is certain to block on screening anyway; the guard still runs and reports every reason.
CLAUDE.md §7: "Checkpoint at every node so an interrupted case resumes rather than restarts" —
compile with a checkpointer and pass a thread id (see continuum.graph.run).
"""

from __future__ import annotations

from typing import Any

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from continuum.graph.deps import GraphDeps
from continuum.graph.nodes.case_assembly import assemble_case
from continuum.graph.nodes.draft import write_draft
from continuum.graph.nodes.guard import guard_case
from continuum.graph.nodes.prioritise import prioritise_case
from continuum.graph.nodes.route import route_case
from continuum.graph.nodes.scoring import score_case
from continuum.graph.nodes.strategy import plan_strategy
from continuum.graph.state import CaseState
from continuum.screening.status import ScreeningState


def _after_assembly(state: CaseState) -> str:
    return "route" if state.route is not None else "scoring"


def _after_prioritise(state: CaseState) -> str:
    assert state.priority is not None and state.case is not None
    if state.priority.divert or not state.priority.worth_contacting:
        return "route"
    if state.case.screening is not ScreeningState.CLEAR:
        return "guard"
    return "strategy"


def build_graph(
    deps: GraphDeps, checkpointer: BaseCheckpointSaver[Any] | None
) -> CompiledStateGraph[CaseState, Any, CaseState, CaseState]:
    graph: StateGraph[CaseState, Any, CaseState, CaseState] = StateGraph(CaseState)
    graph.add_node("case_assembly", lambda s: assemble_case(s, deps))
    graph.add_node("scoring", lambda s: score_case(s, deps))
    graph.add_node("prioritise", lambda s: prioritise_case(s, deps))
    graph.add_node("strategy", lambda s: plan_strategy(s, deps))
    graph.add_node("draft", lambda s: write_draft(s, deps))
    graph.add_node("guard", lambda s: guard_case(s, deps))
    graph.add_node("route", lambda s: route_case(s, deps))

    graph.add_edge(START, "case_assembly")
    graph.add_conditional_edges(
        "case_assembly", _after_assembly, {"route": "route", "scoring": "scoring"}
    )
    graph.add_edge("scoring", "prioritise")
    graph.add_conditional_edges(
        "prioritise",
        _after_prioritise,
        {"route": "route", "guard": "guard", "strategy": "strategy"},
    )
    graph.add_edge("strategy", "draft")
    graph.add_edge("draft", "guard")
    graph.add_edge("guard", "route")
    graph.add_edge("route", END)
    return graph.compile(checkpointer=checkpointer)
