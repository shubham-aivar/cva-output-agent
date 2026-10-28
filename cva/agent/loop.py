"""The agent loop: the model chooses the next tool until it is done or a limit is hit.

Limits (turns, tool calls, tokens) come from settings.  Every model call and tool call is written to the audit
log and the full transcript is kept, so a reviewer can see exactly what the agent looked at and why."""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..review.audit import AuditLog
from ..util import canonical_json, now_iso, sha256_hex
from .llm import LLM
from .tools import ToolBox


@dataclass
class TaskRun:
    task: str
    stop: str = "completed"             # completed | max_turns | max_tool_calls | max_tokens | model_error
    turns: int = 0
    tool_calls: int = 0
    tokens_in: int = 0
    tokens_out: int = 0
    final_text: str = ""
    proposals: list[dict[str, Any]] = field(default_factory=list)
    transcript: list[dict[str, Any]] = field(default_factory=list)
    error: str | None = None

    def summary(self) -> dict[str, Any]:
        return {"task": self.task, "stop": self.stop, "turns": self.turns, "tool_calls": self.tool_calls,
                "proposals_created": len(self.proposals), "summary": self.final_text, "error": self.error}


def run_loop(llm: LLM, system: str, user: str, box: ToolBox, limits: dict[str, Any], audit: AuditLog | None,
             transcript_dir: Path | None = None, actor: str = "agent") -> TaskRun:
    run = TaskRun(task=box.task)
    messages: list[dict[str, Any]] = [{"role": "user", "content": [{"text": user}]}]
    max_turns = int(limits.get("max_turns", 15))
    max_calls = int(limits.get("max_tool_calls", 40))
    max_tokens = int(limits.get("max_tokens", 60000))
    n_before = len(box.ctx.proposals)
    while True:
        if run.turns >= max_turns:
            run.stop = "max_turns"
            break
        if run.tokens_in + run.tokens_out >= max_tokens:
            run.stop = "max_tokens"
            break
        run.turns += 1
        try:
            resp = llm.converse(system, messages, box.specs(), 2000)
        except Exception as e:  # a model failure ends the task cleanly; nothing else is affected
            run.stop, run.error = "model_error", f"{type(e).__name__}: {e}"
            break
        run.tokens_in += resp.input_tokens
        run.tokens_out += resp.output_tokens
        if audit:
            audit.append("llm_call", {"task": box.task, "turn": run.turns, "model": llm.name,
                                      "input_tokens": resp.input_tokens, "output_tokens": resp.output_tokens,
                                      "stop_reason": resp.stop_reason,
                                      "messages_sha256": sha256_hex(canonical_json(messages))}, actor)
        messages.append(resp.to_message())
        if not resp.tool_calls:
            run.final_text = resp.text.strip()
            break
        results: list[dict[str, Any]] = []
        for tc in resp.tool_calls:
            if run.tool_calls >= max_calls:
                run.stop = "max_tool_calls"
                out: dict[str, Any] = {"error": "tool call limit reached"}
            else:
                run.tool_calls += 1
                out = box.call(tc.name, tc.input)
            if audit:
                audit.append("tool_call", {"task": box.task, "tool": tc.name, "args_sha256": sha256_hex(canonical_json(tc.input)),
                                           "error": out.get("error")}, actor)
            results.append({"toolResult": {"toolUseId": tc.id, "content": [{"json": out}],
                                           "status": "error" if "error" in out else "success"}})
        messages.append({"role": "user", "content": results})
        if run.stop == "max_tool_calls":
            break
        if box.ctx.finished is not None and box.task == "orchestrator":
            run.final_text = box.ctx.finished
            break
    run.proposals = box.ctx.proposals[n_before:]
    run.transcript = messages
    if transcript_dir is not None:
        transcript_dir.mkdir(parents=True, exist_ok=True)
        n = len(list(transcript_dir.glob(f"{box.task}-*.json"))) + 1
        f = transcript_dir / f"{box.task}-{n}.json"
        f.write_text(json.dumps({"task": box.task, "model": llm.name, "ts": now_iso(), "summary": run.summary(),
                                 "messages": messages}, indent=1, default=str), encoding="utf-8")
        if audit:
            audit.append("agent_task", {**run.summary(), "transcript": f.name,
                                        "transcript_sha256": sha256_hex(f.read_bytes())}, actor)
    return run
