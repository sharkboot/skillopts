"""Draw.io rollout — single-turn diagram-generation agent + batch execution.

The agent receives the current skill document (as the system prompt) and a
task instruction, then produces a ``.drawio`` XML diagram. The response is
scored deterministically by :mod:`skillopt.envs.drawio.evaluator`.

Public API
----------
- :func:`process_one` — run + evaluate one diagram task
- :func:`run_batch`   — parallel execution of a list of tasks (resume-aware)
"""
from __future__ import annotations

import json
import os
import time
from collections import Counter
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait

from skillopt.envs.drawio.evaluator import evaluate
from skillopt.model import chat_target
from skillopt.prompts import load_prompt


def _raise_on_systemic_failure(results: list[dict]) -> None:
    """Abort when every rollout row failed before any agent response."""
    if not results or not all(row.get("agent_ok") is False for row in results):
        return
    reasons = Counter(str(row.get("fail_reason") or "unknown error") for row in results)
    common_reason, count = reasons.most_common(1)[0]
    raise RuntimeError(
        f"Drawio rollout failed for all {len(results)} items before an agent "
        f"response ({count}x): {common_reason}"
    )


def _build_system(skill_content: str) -> str:
    if skill_content.strip():
        skill_section = f"## Skill\n{skill_content.strip()}\n\n"
    else:
        skill_section = ""
    return load_prompt("rollout_system", env="drawio").format(skill_section=skill_section)


def _build_user(instruction: str, requirements: dict) -> str:
    parts = [f"## Task\n{instruction}"]
    reqs = requirements or {}
    hints: list[str] = []
    if reqs.get("min_nodes"):
        hints.append(f"- 至少包含 {reqs['min_nodes']} 个节点 (vertex)")
    if reqs.get("min_edges"):
        hints.append(f"- 至少包含 {reqs['min_edges']} 条连线 (edge)")
    if reqs.get("required_labels"):
        labels = "、".join(str(x) for x in reqs["required_labels"])
        hints.append(f"- 图中必须出现这些关键标签：{labels}")
    if reqs.get("needs_dashed_edge"):
        hints.append("- 需要用虚线箭头 (dashed=1) 表达残差/跳跃连接")
    if hints:
        parts.append("## Requirements\n" + "\n".join(hints))
    parts.append(
        "## Output\n"
        "在 ```xml 代码块中输出完整、合法的 .drawio XML"
        "（以 <mxfile> 开头、</mxfile> 结尾）。"
    )
    return "\n\n".join(parts)


# ── Single-item execution ─────────────────────────────────────────────────────


def process_one(
    item: dict,
    out_root: str,
    skill_content: str,
    exec_timeout: int = 120,
    max_completion_tokens: int = 8192,
) -> dict:
    """Process a single diagram task: run agent + deterministic eval."""
    item_id = str(item["id"])
    instruction = item.get("instruction", "")
    requirements = item.get("requirements", {}) or {}

    result = {
        "id": item_id,
        "task_description": instruction,
        "task_type": item.get("task_type") or "drawio",
        "hard": 0,
        "soft": 0.0,
        "predicted_answer": "",
        "response": "",
        "checks": {},
        "fail_reason": "",
        "agent_ok": False,
        "requirements": requirements,
    }

    try:
        pred_dir = os.path.join(out_root, "predictions", item_id)
        os.makedirs(pred_dir, exist_ok=True)

        system = _build_system(skill_content)
        user = _build_user(instruction, requirements)

        response, _usage = chat_target(
            system=system,
            user=user,
            max_completion_tokens=max_completion_tokens,
            retries=5,
            stage="rollout",
            timeout=exec_timeout,
        )

        result["response"] = response
        result["agent_ok"] = True

        eval_result = evaluate(response, requirements)
        result["hard"] = int(eval_result["hard"])
        result["soft"] = float(eval_result["soft"])
        result["checks"] = eval_result["checks"]
        result["predicted_answer"] = eval_result.get("xml", "")[:2000]
        result["n_nodes"] = eval_result.get("n_nodes", 0)
        result["n_edges"] = eval_result.get("n_edges", 0)
        if eval_result["hard"] < 1:
            result["fail_reason"] = "; ".join(eval_result["fail_reasons"]) or "checks failed"

        # Persist artifacts for the analyst / debugging.
        with open(os.path.join(pred_dir, "target_system_prompt.txt"), "w", encoding="utf-8") as f:
            f.write(system)
        with open(os.path.join(pred_dir, "target_user_prompt.txt"), "w", encoding="utf-8") as f:
            f.write(user)
        with open(os.path.join(pred_dir, "response.txt"), "w", encoding="utf-8") as f:
            f.write(response)
        if eval_result.get("xml"):
            with open(os.path.join(pred_dir, "diagram.drawio"), "w", encoding="utf-8") as f:
                f.write(eval_result["xml"])

        eval_detail = (
            f"[EVALUATION RESULT]\n"
            f"Task: {instruction}\n"
            f"Checks: {json.dumps(eval_result['checks'], ensure_ascii=False)}\n"
            f"Nodes: {eval_result.get('n_nodes', 0)}  Edges: {eval_result.get('n_edges', 0)}\n"
            f"hard={eval_result['hard']}  soft={eval_result['soft']:.4f}\n"
            f"Fail reasons: {eval_result['fail_reasons']}"
        )
        conversation = [
            {"type": "message", "turn": 1, "content": response},
            {"role": "system", "content": eval_detail},
        ]
        with open(os.path.join(pred_dir, "conversation.json"), "w", encoding="utf-8") as f:
            json.dump(conversation, f, ensure_ascii=False, indent=2)

    except Exception as e:  # noqa: BLE001
        result["fail_reason"] = f"error: {e}"

    return result


# ── Batch execution ────────────────────────────────────────────────────────────


def run_batch(
    items: list[dict],
    out_root: str,
    skill_content: str,
    exec_timeout: int = 120,
    workers: int = 8,
    max_completion_tokens: int = 8192,
    task_timeout: int = 600,
    **_kwargs,
) -> list[dict]:
    """Run the diagram agent on all items with a thread pool. Resume-aware."""
    task_timeout = max(int(task_timeout), int(exec_timeout) + 60)
    results_path = os.path.join(out_root, "results.jsonl")
    os.makedirs(out_root, exist_ok=True)

    done_ids: set[str] = set()
    existing: list[dict] = []
    if os.path.exists(results_path):
        with open(results_path, encoding="utf-8") as f:
            for line in f:
                try:
                    r = json.loads(line)
                    done_ids.add(str(r["id"]))
                    existing.append(r)
                except Exception:
                    pass

    pending = [it for it in items if str(it["id"]) not in done_ids]
    if not pending:
        _raise_on_systemic_failure(existing)
        return existing

    total = len(existing) + len(pending)
    completed = len(existing)
    correct_count = sum(1 for r in existing if r.get("hard", 0))
    if existing:
        print(f"    [rollout] resuming: {completed}/{total} already done", flush=True)

    results = list(existing)

    def _timeout_result(item: dict) -> dict:
        return {
            "id": str(item["id"]),
            "task_description": item.get("instruction", ""),
            "task_type": item.get("task_type") or "drawio",
            "hard": 0,
            "soft": 0.0,
            "predicted_answer": "",
            "response": "",
            "fail_reason": f"task-timeout-{task_timeout}s",
            "agent_ok": False,
            "requirements": item.get("requirements", {}),
            "phase": "timeout",
        }

    def _error_result(item: dict, exc: Exception) -> dict:
        row = _timeout_result(item)
        row["phase"] = "error"
        row["fail_reason"] = f"unexpected: {type(exc).__name__}: {exc}"
        return row

    started_at: dict[str, float] = {}

    def _run_one(item: dict) -> dict:
        started_at[str(item["id"])] = time.time()
        return process_one(
            item,
            out_root,
            skill_content,
            exec_timeout,
            max_completion_tokens,
        )

    with open(results_path, "a", encoding="utf-8") as outf:
        ex = ThreadPoolExecutor(max_workers=workers)
        try:
            futs = {ex.submit(_run_one, it): it for it in pending}
            pending_futs = set(futs)
            while pending_futs:
                done, _ = wait(pending_futs, timeout=5, return_when=FIRST_COMPLETED)
                now = time.time()
                timed_out = [
                    fut for fut in pending_futs - done
                    if str(futs[fut]["id"]) in started_at
                    and now - started_at[str(futs[fut]["id"])] >= task_timeout
                ]
                for fut in done:
                    pending_futs.remove(fut)
                    item = futs[fut]
                    try:
                        res = fut.result()
                    except Exception as exc:  # noqa: BLE001
                        res = _error_result(item, exc)
                    results.append(res)
                    completed += 1
                    if res.get("hard", 0):
                        correct_count += 1
                    acc = correct_count / completed if completed else 0
                    print(
                        f"    [rollout] {completed}/{total} "
                        f"(acc={acc:.3f}) id={res['id']} "
                        f"hard={res.get('hard', '?')} soft={res.get('soft', 0):.2f}",
                        flush=True,
                    )
                    outf.write(json.dumps(res, ensure_ascii=False) + "\n")
                    outf.flush()
                for fut in timed_out:
                    pending_futs.remove(fut)
                    fut.cancel()
                    res = _timeout_result(futs[fut])
                    results.append(res)
                    completed += 1
                    acc = correct_count / completed if completed else 0
                    print(
                        f"    [rollout] {completed}/{total} "
                        f"(acc={acc:.3f}) id={res['id']} TIMEOUT",
                        flush=True,
                    )
                    outf.write(json.dumps(res, ensure_ascii=False) + "\n")
                    outf.flush()
        finally:
            ex.shutdown(wait=False, cancel_futures=True)

    _raise_on_systemic_failure(results)
    return results
