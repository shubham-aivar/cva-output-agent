"""Run the agent tasks.  ``run_task`` runs one specialist; ``orchestrate`` lets the model decide which ones."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from ..config import config_dir
from ..review.audit import AuditLog
from .llm import LLM
from .loop import TaskRun, run_loop
from .tools import AgentContext, ToolBox

TASKS = ("intake", "licensing", "parity", "narrative", "orchestrator")
USER_MSG = {
    "intake": "Map the missing required inputs if you can.",
    "licensing": "Review the licensing gaps for this run.",
    "parity": "Review the parity outliers for this run.",
    "narrative": "Draft the slide commentary for this run.",
    "orchestrator": "Decide which tasks this run needs, run them, then finish.",
}


def _prompt(task: str) -> str:
    d = config_dir() / "prompts"
    return (d / "common.md").read_text(encoding="utf-8") + "\n\n" + (d / f"{task}.md").read_text(encoding="utf-8")


def run_task(task: str, ctx: AgentContext, llm: LLM, limits: dict[str, Any], audit: AuditLog | None,
             transcript_dir: Path | None) -> TaskRun:
    if task not in TASKS:
        raise ValueError(f"unknown task '{task}'")
    ctx.llm_label = llm.name
    box = ToolBox(ctx, task)
    return run_loop(llm, _prompt(task), USER_MSG[task], box, limits, audit, transcript_dir)


def orchestrate(ctx: AgentContext, llm: LLM, limits: dict[str, Any], audit: AuditLog | None,
                transcript_dir: Path | None) -> dict[str, Any]:
    """The orchestrator is itself an agent loop whose only tools are get_run_state, run_task and finish."""
    sub_runs: list[dict[str, Any]] = []

    def sub(name: str) -> dict[str, Any]:
        r = run_task(name, ctx, llm, limits, audit, transcript_dir)
        sub_runs.append(r.summary())
        return r.summary()

    ctx.run_subtask = sub
    ctx.llm_label = llm.name
    box = ToolBox(ctx, "orchestrator")
    top = run_loop(llm, _prompt("orchestrator"), USER_MSG["orchestrator"], box, {**limits, "max_turns": 10},
                   audit, transcript_dir)
    return {"orchestrator": top.summary(), "tasks": sub_runs, "proposals": [p["id"] for p in ctx.proposals]}
