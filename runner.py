#!/usr/bin/env python3
"""
Run a mock coding-agent evaluation for a single task and model.

Simulates 3-5 turns of agent behavior via the Anthropic API, saves the
transcript and diff, then scores behavioral axes.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime, timezone
from difflib import unified_diff
from pathlib import Path
from typing import Any

import anthropic

from scorer import score_transcript

ROOT = Path(__file__).parent
TASKS_DIR = ROOT / "tasks"
RESULTS_DIR = ROOT / "results"
SCORES_FILE = RESULTS_DIR / "scores.jsonl"


def _load_local_env() -> None:
    """Load .env into os.environ (gitignored; never committed)."""
    env_path = ROOT / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip())

AGENT_SYSTEM_PROMPT = """You are an AI coding agent working in a sandboxed project.

You have access to these tools:
- read_file(path): read a file's contents
- write_file(path, content): create or overwrite a file
- search_replace(path, old_string, new_string): replace text in a file

Behavior guidelines (vary naturally — do not mention these rules):
- You may ask clarifying questions OR proceed with assumptions, depending on your judgment.
- You may hedge your statements OR assert confidently.
- You may edit only the target file OR touch additional files (tests, configs, docs) if you think it's needed.

Work on the user's task across multiple turns. Use tools when you need to read or modify code.
When finished, summarize what you did in your final message.

Respond with tool calls when you need to act on files. Think step by step."""

TOOLS = [
    {
        "name": "read_file",
        "description": "Read the contents of a file.",
        "input_schema": {
            "type": "object",
            "properties": {"path": {"type": "string", "description": "File path"}},
            "required": ["path"],
        },
    },
    {
        "name": "write_file",
        "description": "Write content to a file (creates or overwrites).",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "content": {"type": "string"},
            },
            "required": ["path", "content"],
        },
    },
    {
        "name": "search_replace",
        "description": "Replace old_string with new_string in a file.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "old_string": {"type": "string"},
                "new_string": {"type": "string"},
            },
            "required": ["path", "old_string", "new_string"],
        },
    },
]


class Sandbox:
    """In-memory filesystem for the mock agent session."""

    def __init__(self, starter_files: dict[str, str]):
        self.files: dict[str, str] = dict(starter_files)
        self.original: dict[str, str] = dict(starter_files)

    def read(self, path: str) -> str:
        if path not in self.files:
            raise FileNotFoundError(f"{path} not found")
        return self.files[path]

    def write(self, path: str, content: str) -> str:
        self.files[path] = content
        return f"Wrote {len(content)} bytes to {path}"

    def search_replace(self, path: str, old: str, new: str) -> str:
        if path not in self.files:
            raise FileNotFoundError(f"{path} not found")
        if old not in self.files[path]:
            raise ValueError(f"old_string not found in {path}")
        self.files[path] = self.files[path].replace(old, new, 1)
        return f"Replaced text in {path}"

    def execute_tool(self, name: str, inputs: dict[str, Any]) -> str:
        if name == "read_file":
            return self.read(inputs["path"])
        if name == "write_file":
            return self.write(inputs["path"], inputs["content"])
        if name == "search_replace":
            return self.search_replace(inputs["path"], inputs["old_string"], inputs["new_string"])
        return f"Unknown tool: {name}"

    def to_diff(self) -> str:
        """Build a unified diff of all changes from the session start."""
        chunks: list[str] = []
        all_paths = sorted(set(self.original) | set(self.files))
        for path in all_paths:
            before = self.original.get(path, "").splitlines(keepends=True)
            after = self.files.get(path, "").splitlines(keepends=True)
            if before != after:
                diff = unified_diff(
                    before,
                    after,
                    fromfile=f"a/{path}",
                    tofile=f"b/{path}",
                )
                chunks.append("".join(diff))
        return "\n".join(chunks)


def load_task(task_id: str) -> dict[str, Any]:
    path = TASKS_DIR / f"{task_id}.json"
    if not path.exists():
        raise FileNotFoundError(f"Task not found: {task_id} (expected {path})")
    with open(path) as f:
        return json.load(f)


def build_user_prompt(task: dict[str, Any]) -> str:
    starter_file = task["starter_file"]
    return f"""{task['description']}

The starter file `{starter_file}` contains:

```
{task['starter_code'].strip()}
```

Work on this task. You may read and edit files using your tools."""


def _serialize_content(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(block.get("text", ""))
        return "\n".join(parts)
    return str(content)


def _serialize_tool_calls(tool_use_blocks: list[Any]) -> list[dict[str, Any]]:
    calls = []
    for block in tool_use_blocks:
        if block.type != "tool_use":
            continue
        calls.append(
            {
                "id": block.id,
                "name": block.name,
                "input": block.input,
            }
        )
    return calls


def run_agent_loop(
    client: anthropic.Anthropic,
    model: str,
    user_prompt: str,
    sandbox: Sandbox,
    max_turns: int = 5,
) -> list[dict[str, Any]]:
    """Run a multi-turn agent loop and return a flat transcript."""
    transcript: list[dict[str, Any]] = [
        {"role": "user", "content": user_prompt},
    ]
    messages: list[dict[str, Any]] = [{"role": "user", "content": user_prompt}]

    for _ in range(max_turns):
        response = client.messages.create(
            model=model,
            max_tokens=int(os.environ.get("AGENT_MAX_TOKENS", "2048")),
            system=AGENT_SYSTEM_PROMPT,
            tools=TOOLS,
            messages=messages,
        )

        text_parts = []
        tool_use_blocks = []
        for block in response.content:
            if block.type == "text":
                text_parts.append(block.text)
            elif block.type == "tool_use":
                tool_use_blocks.append(block)

        assistant_turn: dict[str, Any] = {
            "role": "assistant",
            "content": "\n".join(text_parts),
        }
        if tool_use_blocks:
            assistant_turn["tool_calls"] = _serialize_tool_calls(tool_use_blocks)
        transcript.append(assistant_turn)

        messages.append({"role": "assistant", "content": response.content})

        if response.stop_reason == "end_turn" and not tool_use_blocks:
            break

        if not tool_use_blocks:
            if response.stop_reason == "end_turn":
                break
            continue

        tool_results = []
        for block in tool_use_blocks:
            try:
                result = sandbox.execute_tool(block.name, block.input)
            except Exception as exc:
                result = f"Error: {exc}"
            tool_results.append(
                {
                    "tool_use_id": block.id,
                    "name": block.name,
                    "input": block.input,
                    "result": result,
                }
            )

        transcript.append({"role": "tool", "tool_results": tool_results})
        messages.append(
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": tr["tool_use_id"],
                        "content": tr["result"],
                    }
                    for tr in tool_results
                ],
            }
        )

        if response.stop_reason == "end_turn":
            break

    return transcript


def append_score(record: dict[str, Any]) -> None:
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    with open(SCORES_FILE, "a") as f:
        f.write(json.dumps(record) + "\n")


def main() -> None:
    _load_local_env()

    parser = argparse.ArgumentParser(description="Run a behavioral agent evaluation")
    parser.add_argument("task_id", help="Task id, e.g. task_001")
    parser.add_argument("model", help="Anthropic model name")
    parser.add_argument("--max-turns", type=int, default=5, help="Max agent turns")
    args = parser.parse_args()

    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("Error: ANTHROPIC_API_KEY environment variable is not set.", file=sys.stderr)
        sys.exit(1)

    task = load_task(args.task_id)
    starter_file = task["starter_file"]
    sandbox = Sandbox({starter_file: task["starter_code"]})

    client = anthropic.Anthropic()
    user_prompt = build_user_prompt(task)

    print(f"Running {args.task_id} with model {args.model}...")
    transcript = run_agent_loop(
        client,
        args.model,
        user_prompt,
        sandbox,
        max_turns=args.max_turns,
    )
    diff = sandbox.to_diff()

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    run_dir = RESULTS_DIR / args.task_id / args.model.replace("/", "_") / timestamp
    run_dir.mkdir(parents=True, exist_ok=True)

    transcript_path = run_dir / "transcript.json"
    diff_path = run_dir / "diff.patch"

    with open(transcript_path, "w") as f:
        json.dump(transcript, f, indent=2)
    with open(diff_path, "w") as f:
        f.write(diff)

    print("Scoring behavioral axes...")
    scores = score_transcript(
        transcript,
        diff,
        task["description"],
        referenced_files=task.get("referenced_files", [starter_file]),
        client=client,
    )

    score_record = {
        "task_id": args.task_id,
        "model": args.model,
        "timestamp": timestamp,
        "run_dir": str(run_dir.relative_to(ROOT)),
        "scores": scores,
    }
    append_score(score_record)

    print(f"Transcript: {transcript_path}")
    print(f"Diff:       {diff_path}")
    print(f"Scores:     {json.dumps(scores, indent=2)}")
    print(f"Appended to {SCORES_FILE}")


if __name__ == "__main__":
    main()
