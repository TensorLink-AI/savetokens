"""How you iterate with coding agents, from your own transcripts: the numbers that tune the
simulated developer. Counts and timings only: no message text is printed or saved.

  python3 evals/iterate/calibrate.py [--out ~/.savetokens/iterate-profile.json] [--gap-min 60]

Sources: Claude Code (~/.claude/projects) and interactive Codex sessions (~/.codex/sessions;
`codex exec` runs and subagent sessions are skipped). A task is the prompts between session starts,
/clear, and (with --gap-min) a pause longer than that many minutes. Each follow-up prompt is
sorted by simple text rules into:

  correction   says something is wrong or still failing, asks to undo, or pastes an error
  approval     short go-ahead: ok, yes, go, do it, continue, looks good
  question     ends with a question mark
  steer        anything else: more detail, a new requirement, the next step

The rules are rough by design; the share in each group and the timings are what matter.
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import re
import statistics
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

CORRECTION = re.compile(
    r"\b(no|nope|wrong|incorrect|doesn'?t work|didn'?t work|not working|still (fails?|failing|broken|not|doesn'?t|wrong|the same)"
    r"|that'?s not|isn'?t right|revert|undo|roll ?back|you broke|(it'?s|is|now) broken|try again|redo|not what i"
    r"|why did you|you (missed|forgot|didn'?t)|same (error|problem|issue)|(still|again) (an? )?(error|bug|issue))\b", re.I)
PASTED_ERROR = re.compile(r"Traceback \(most recent call last\)|^\s*\w*Error: |^error(\[\w+\])?: |FAILED |panicked at", re.M)
APPROVAL = re.compile(r"^\s*(ok(ay)?|yes|yep|yeah|sure|go( ahead)?|do it|lets? do it|let'?s go|continue|proceed|"
                      r"looks good|lgtm|great|perfect|thanks?( you)?|push( it)?|ship it|commit( it)?)\b.{0,40}$", re.I)
SKIP_PREFIX = ("<task-notification>", "<local-command-stdout>", "<bash-stdout>", "<bash-stderr>", "<system-reminder>",
               "Caveat:", "This session is being continued")
INTERRUPT = "[Request interrupted by user"


def _ts(v):
    try:
        return datetime.fromisoformat(str(v).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _text(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text")
    return ""


def claude_sessions(root):
    """Per session: a time-ordered list of (ts, kind, text) with kind prompt | command:<name> | interrupt | agent."""
    for f in glob.glob(str(Path(root).expanduser() / "*" / "*.jsonl")):
        if "st-eval" in f:
            continue
        evs = []
        for line in open(f, errors="ignore"):
            try:
                d = json.loads(line)
            except ValueError:
                continue
            ts = _ts(d.get("timestamp"))
            if ts is None or d.get("isSidechain"):
                continue
            if d.get("type") == "assistant":
                evs.append((ts, "agent", ""))
                continue
            if d.get("type") != "user" or d.get("isMeta") or d.get("isCompactSummary") or d.get("toolUseResult"):
                continue
            text = _text((d.get("message") or {}).get("content"))
            if not text.strip():
                continue
            if text.startswith(INTERRUPT):
                evs.append((ts, "interrupt", ""))
            elif text.startswith("<command-name>"):
                m = re.match(r"<command-name>/?([\w:-]+)", text)
                evs.append((ts, f"command:{m.group(1) if m else '?'}", ""))
            elif text.startswith("<bash-input>"):
                evs.append((ts, "command:!shell", ""))
            elif not text.startswith(SKIP_PREFIX):
                evs.append((ts, "prompt", text))
        if evs:
            yield "claude-code", sorted(evs, key=lambda e: e[0])


def codex_sessions(root):
    for f in glob.glob(str(Path(root).expanduser() / "**" / "*.jsonl"), recursive=True):
        evs, interactive = [], None
        for line in open(f, errors="ignore"):
            try:
                d = json.loads(line)
            except ValueError:
                continue
            p = d.get("payload") or {}
            if d.get("type") == "session_meta":
                # `codex exec` runs and subagents (source {"subagent": ...}) are not a person typing
                interactive = p.get("originator") != "codex_exec" and not isinstance(p.get("source"), dict)
                continue
            ts = _ts(d.get("timestamp"))
            if ts is None:
                continue
            if d.get("type") == "event_msg" and p.get("type") == "user_message":
                text = p.get("message") or ""
                if text.strip().startswith("/"):
                    evs.append((ts, f"command:{text.strip().split()[0][1:]}", ""))
                elif text.strip():
                    evs.append((ts, "prompt", text))
            elif d.get("type") == "event_msg" and p.get("type") == "turn_aborted":
                evs.append((ts, "interrupt", ""))
            elif d.get("type") == "response_item" and p.get("role") == "assistant":
                evs.append((ts, "agent", ""))
        if evs and interactive:
            yield "codex", sorted(evs, key=lambda e: e[0])


def classify(text):
    if PASTED_ERROR.search(text) or CORRECTION.search(text[:300]):
        return "correction"
    if len(text) < 80 and APPROVAL.match(text.strip()):
        return "approval"
    if text.rstrip().endswith("?"):
        return "question"
    return "steer"


def tasks_of(evs, gap_min):
    """Split a session into tasks; each task is a list of events from its first prompt."""
    out, cur, last_prompt = [], [], None
    for e in evs:
        ts, kind, _ = e
        boundary = kind in ("command:clear", "command:new") or (
            kind == "prompt" and gap_min and last_prompt and ts - last_prompt > gap_min * 60)
        if boundary and cur:
            out.append(cur)
            cur = []
        if kind == "prompt":
            last_prompt = ts
        if kind == "prompt" or cur:
            cur.append(e)
    if cur:
        out.append(cur)
    return [t for t in out if any(k == "prompt" for _, k, _ in t)]


def _q(xs, p):
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(p * (len(xs) - 1)))] if xs else None


def summarise(sessions, gap_min):
    by = defaultdict(lambda: {"sessions": 0, "tasks": [], "commands": Counter()})
    for harness, evs in sessions:
        h = by[harness]
        h["sessions"] += 1
        for k in (k for _, k, _ in evs if k.startswith("command:")):
            h["commands"][k.split(":", 1)[1]] += 1
        for t in tasks_of(evs, gap_min):
            prompts = [(ts, txt) for ts, k, txt in t if k == "prompt"]
            kinds = [classify(txt) for _, txt in prompts[1:]]
            review, work = [], []
            for i, (ts, k, _) in enumerate(t):
                if k != "prompt":
                    continue
                prev_agent = next((x for x, kk, _ in reversed(t[:i]) if kk == "agent"), None)
                if prev_agent is not None and i > 0:
                    review.append(ts - prev_agent)        # agent's last word to your next prompt
                nxt = next((j for j in range(i + 1, len(t)) if t[j][1] == "prompt"), len(t))
                agents = [x for x, kk, _ in t[i + 1:nxt] if kk == "agent"]
                if agents:
                    work.append(agents[-1] - ts)          # your prompt to the agent's last word
            h["tasks"].append({"prompts": len(prompts), "kinds": kinds, "review_s": review, "work_s": work,
                               "first_words": len(prompts[0][1].split()),
                               "follow_words": [len(x.split()) for _, x in prompts[1:]],
                               "interrupts": sum(1 for _, k, _ in t if k == "interrupt"),
                               "ends_on_correction": bool(kinds) and kinds[-1] == "correction",
                               "start": prompts[0][0]})
    out = {}
    for harness, h in by.items():
        ts = h["tasks"]
        if not ts:
            continue
        n = [t["prompts"] for t in ts]
        kinds = Counter(k for t in ts for k in t["kinds"])
        follow = sum(kinds.values()) or 1
        review = [x for t in ts for x in t["review_s"] if 0 <= x < 6 * 3600]
        work = [x for t in ts for x in t["work_s"] if 0 <= x < 6 * 3600]
        corr = [sum(k == "correction" for k in t["kinds"]) for t in ts]
        out[harness] = {
            "sessions": h["sessions"], "tasks": len(ts),
            "first": datetime.fromtimestamp(min(t["start"] for t in ts)).date().isoformat(),
            "last": datetime.fromtimestamp(max(t["start"] for t in ts)).date().isoformat(),
            "prompts_per_task": {"mean": round(statistics.fmean(n), 2), "p50": _q(n, .5), "p90": _q(n, .9),
                                 "max": max(n), "share_one_prompt": round(sum(x == 1 for x in n) / len(n), 3)},
            "follow_ups": {k: round(v / follow, 3) for k, v in kinds.most_common()},
            "corrections_per_task": {"mean": round(statistics.fmean(corr), 2),
                                     "share_with_any": round(sum(c > 0 for c in corr) / len(corr), 3)},
            "share_ending_on_correction": round(sum(t["ends_on_correction"] for t in ts) / len(ts), 3),
            "interrupts_per_task": round(statistics.fmean(t["interrupts"] for t in ts), 3),
            "review_minutes": {"p50": round(_q(review, .5) / 60, 1), "p90": round(_q(review, .9) / 60, 1)} if review else None,
            "agent_minutes_per_turn": {"p50": round(_q(work, .5) / 60, 1), "p90": round(_q(work, .9) / 60, 1)} if work else None,
            "words": {"first_p50": _q([t["first_words"] for t in ts], .5),
                      "follow_up_p50": _q([w for t in ts for w in t["follow_words"]], .5)},
            "commands": dict(h["commands"].most_common(12)),
        }
    return out


def render(p, gap_min):
    lines = [f"tasks split at session starts and /clear" + (f", and pauses over {gap_min} min" if gap_min else "")]
    for h, s in p.items():
        f = s["follow_ups"]
        lines += [
            f"\n{h}: {s['tasks']} tasks in {s['sessions']} sessions, {s['first']} – {s['last']}",
            f"  prompts per task: mean {s['prompts_per_task']['mean']}, median {s['prompts_per_task']['p50']},"
            f" p90 {s['prompts_per_task']['p90']}, max {s['prompts_per_task']['max']};"
            f" {s['prompts_per_task']['share_one_prompt']:.0%} are a single prompt",
            "  follow-ups: " + ", ".join(f"{k} {v:.0%}" for k, v in f.items()),
            f"  corrections: {s['corrections_per_task']['mean']} per task; {s['corrections_per_task']['share_with_any']:.0%}"
            f" of tasks have one; {s['share_ending_on_correction']:.0%} end right after one (possible give-ups)",
            f"  interrupts: {s['interrupts_per_task']} per task",
        ]
        if s["review_minutes"]:
            lines.append(f"  your time between the agent's answer and your next prompt: median {s['review_minutes']['p50']} min,"
                         f" p90 {s['review_minutes']['p90']} min")
        if s["agent_minutes_per_turn"]:
            lines.append(f"  agent time per turn: median {s['agent_minutes_per_turn']['p50']} min,"
                         f" p90 {s['agent_minutes_per_turn']['p90']} min")
        lines.append(f"  words: first prompt median {s['words']['first_p50']}, follow-ups median {s['words']['follow_up_p50']}")
        lines.append("  commands: " + ", ".join(f"/{k} {v}" for k, v in s["commands"].items()))
    return "\n".join(lines)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--claude-root", default=os.environ.get("CLAUDE_CONFIG_DIR", "~/.claude") + "/projects")
    ap.add_argument("--codex-root", default=os.environ.get("CODEX_HOME", "~/.codex") + "/sessions")
    ap.add_argument("--gap-min", type=float, default=0, help="also split tasks at pauses longer than this")
    ap.add_argument("--out", default="~/.savetokens/iterate-profile.json")
    a = ap.parse_args(argv)
    sessions = list(claude_sessions(a.claude_root)) + list(codex_sessions(a.codex_root))
    p = summarise(sessions, a.gap_min)
    print(render(p, a.gap_min))
    out = Path(a.out).expanduser()
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({"gap_min": a.gap_min, "made": datetime.now().isoformat(), "harnesses": p}, indent=2))
    print(f"\nsaved {out} (counts only)")


if __name__ == "__main__":
    main()
