"""Write the Harbor job configs for SWE-bench Verified Mini: one job per arm, same tasks.

  python3 evals/swebench/make_jobs.py --model glm-5.3-flash --fallback-config ~/st-bench/fallback.yaml \
      --attempts 1 --concurrent 4 [--tasks 2] [--out ~/st-bench]

Tasks are the 50 of SWE-bench Verified Mini (MariusHobbhahn/swe-bench-verified-mini, a published
subset that keeps the full set's pass rates and difficulty), run from Harbor's swebench-verified
dataset. --tasks N keeps the first N (sorted) for a smoke test. Arms:

  off   Hermes as configured, with the fallback model
  on    the same, plus savetokens (mode lean, Hermes levers allowed)

The model goes through Hermes's OpenAI-compatible provider: set OPENAI_API_KEY and OPENAI_BASE_URL
(the gateway, e.g. https://api.engy.ai/v1) in the environment that runs `harbor run`.
"""
from __future__ import annotations

import argparse
import json
import urllib.request
from pathlib import Path

MINI = ("https://datasets-server.huggingface.co/rows?dataset=MariusHobbhahn%2Fswe-bench-verified-mini"
        "&config=default&split=test&offset=0&length=100")


def mini_ids(ids_file=None):
    if ids_file:
        return sorted(x.strip() for x in Path(ids_file).read_text().split() if x.strip())
    with urllib.request.urlopen(MINI, timeout=60) as r:
        rows = json.load(r)["rows"]
    return sorted(row["row"]["instance_id"] for row in rows)


def job(name, model, ids, attempts, concurrent, out, fallback, savetokens, registry_path=None):
    kwargs = {"savetokens": savetokens}
    if fallback:
        kwargs["extra_config"] = str(Path(fallback).expanduser().resolve())
    return {"job_name": name, "jobs_dir": str(Path(out).expanduser().resolve() / "jobs"),
            "n_attempts": attempts, "n_concurrent_trials": concurrent,
            "agents": [{"import_path": "hermes_st:HermesST", "model_name": f"openai/{model}", "kwargs": kwargs}],
            "datasets": [{"name": "swebench-verified", "version": "1.0", "task_names": ids,
                          **({"registry_path": str(Path(registry_path).expanduser().resolve())}
                             if registry_path else {})}]}


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", required=True, help="the gateway's model id, e.g. glm-5.3-flash")
    ap.add_argument("--fallback-config", help="YAML fragment appended to Hermes's config (no keys in it)")
    ap.add_argument("--attempts", type=int, default=1)
    ap.add_argument("--concurrent", type=int, default=4)
    ap.add_argument("--tasks", type=int, help="first N tasks only (smoke test)")
    ap.add_argument("--ids-file", help="instance ids, one per line, instead of fetching the Mini list")
    ap.add_argument("--registry-path", help="registry.json to resolve the dataset from, if Harbor's hub is unreachable")
    ap.add_argument("--tag", default="", help="suffix for job names, e.g. -smoke")
    ap.add_argument("--out", default="~/st-bench")
    a = ap.parse_args(argv)
    ids = mini_ids(a.ids_file)
    if len(ids) != 50 and not a.ids_file:
        raise SystemExit(f"expected 50 Verified Mini ids, got {len(ids)}")
    ids = ids[:a.tasks] if a.tasks else ids
    out = Path(a.out).expanduser()
    out.mkdir(parents=True, exist_ok=True)
    (out / "mini-ids.txt").write_text("\n".join(ids) + "\n")
    for arm, st in (("off", False), ("on", True)):
        cfg = job(f"st-{arm}{a.tag}", a.model, ids, a.attempts, a.concurrent, out, a.fallback_config, st, a.registry_path)
        path = out / f"job-{arm}{a.tag}.json"
        path.write_text(json.dumps(cfg, indent=2))
        print(f"{path}: {len(ids)} tasks x {a.attempts} attempt(s), savetokens {'on' if st else 'off'}")


if __name__ == "__main__":
    main()
