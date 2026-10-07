---
name: savetokens
description: Check token budget, rate limits (5-hour and weekly), spend and waste with savetokens; set the quality/savings mode. Use when the user mentions tokens, cost, spend, usage limit, rate limit, running out, expensive or runaway sessions, or before starting a large job (many subagents, wide refactors, long test loops).
---

# savetokens

savetokens is installed on this machine. It records usage locally and forecasts limits.

Before a large job, check the budget:

```sh
savetokens budget --json
```

It returns the mode, each limit's `used` and `forecast` (p10, p50, p90 % at reset), when it resets,
and `advice` when work should be economical. If a limit is forecast at or above 100% before it
resets, tell the user, and prefer a plan that finishes inside the budget (smaller steps, targeted
reads and tests, a cheaper subagent model) over one that stalls halfway.

Other commands:

| Command | Use |
| --- | --- |
| `savetokens status` | Actuals and forecasts for the hour, 5-hour window, day and week |
| `savetokens report` | Where tokens went and the biggest waste |
| `savetokens mode [quality\|balanced\|lean\|auto]` | Show or set the quality knob (ask the user before changing it) |
| `savetokens fixes list` | Settings fixes; apply only with the user's consent |

The mode is the user's choice: `quality` never asks for economy, `lean` always does, `auto`
economises only when a limit is at risk. Never lower quality on your own to save tokens; when
the budget is tight, say so and let the user choose.
