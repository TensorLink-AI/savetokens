"""Iteration profile: follow-up classes and task splitting (no transcript text leaves the machine)."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "evals" / "iterate"))
import calibrate  # noqa: E402


def test_classify_follow_ups():
    assert calibrate.classify("no, that's wrong, the total should include tax") == "correction"
    assert calibrate.classify("Traceback (most recent call last):\n  File x\nValueError: bad") == "correction"
    assert calibrate.classify("ok do it") == "approval"
    assert calibrate.classify("why is the cache keyed by origin?") == "question"
    assert calibrate.classify("fix the bug in the parser and add a test") == "steer"   # a new request, not a correction


def test_tasks_split_at_clear_and_long_pauses():
    evs = [(0, "prompt", "a"), (10, "agent", ""), (20, "prompt", "b"), (30, "agent", ""),
           (40, "command:clear", ""), (50, "prompt", "c"), (60, "agent", ""), (60 + 2 * 3600, "prompt", "d")]
    assert [len([e for e in t if e[1] == "prompt"]) for t in calibrate.tasks_of(evs, 0)] == [2, 2]
    assert [len([e for e in t if e[1] == "prompt"]) for t in calibrate.tasks_of(evs, 60)] == [2, 1, 1]
