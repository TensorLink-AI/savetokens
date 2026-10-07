"""Harness-neutral classification of tool calls: kind, target and an argument hash."""
from __future__ import annotations

import hashlib
import json
import re

READ = {"read", "read_file", "notebookread", "view", "cat"}
EDIT = {"edit", "write", "multiedit", "notebookedit", "write_file", "patch", "apply_patch", "str_replace_editor",
        "create_file", "edit_file"}
SHELL = {"bash", "terminal", "shell", "exec_command", "run_command", "local_shell"}
SEARCH = {"grep", "glob", "search_files", "ls", "list_files", "find"}
AGENT = {"task", "agent", "delegate_task", "spawn_agent"}

TEST_RE = re.compile(
    r"(?:^|[\s;&|(/])(?:pytest|py\.test|python3? -m (?:pytest|unittest)|tox|nox|jest|vitest|mocha|"
    r"go test|cargo (?:test|nextest)|(?:npm|pnpm|yarn|bun)(?: run)? test|rspec|phpunit|mvn (?:-\S+ )*test|"
    r"gradlew? test|ctest|make (?:test|check)|dotnet test|mix test|swift test)\b")


def _norm(value):
    if isinstance(value, str):
        return " ".join(value.split())
    if isinstance(value, dict):
        return {k: _norm(v) for k, v in sorted(value.items())}
    if isinstance(value, list):
        return [_norm(v) for v in value]
    return value


def args_hash(args) -> str:
    """Hash of whitespace-normalised arguments: near-identical calls hash the same."""
    blob = json.dumps(_norm(args or {}), sort_keys=True, default=str)
    return hashlib.sha1(blob.encode()).hexdigest()[:16]


def command_of(args) -> str:
    if not isinstance(args, dict):
        return ""
    cmd = args.get("command") or args.get("cmd") or ""
    if isinstance(cmd, list):
        cmd = " ".join(map(str, cmd))
    return str(cmd)


def target_of(args):
    if not isinstance(args, dict):
        return None
    for key in ("file_path", "path", "notebook_path", "filename", "file"):
        if isinstance(args.get(key), str):
            return args[key]
    return None


def classify(tool: str, args) -> tuple[str, str | None]:
    """(kind, target) for a tool call. Shell test runs are kind 'test'."""
    name = (tool or "").lower().rsplit("__", 1)[-1]
    if name in READ:
        return "read", target_of(args)
    if name in EDIT:
        return "edit", target_of(args)
    if name in SHELL:
        return ("test" if TEST_RE.search(command_of(args)) else "bash"), None
    if name in SEARCH:
        return "search", None
    if name in AGENT:
        return "agent", None
    return "other", None
