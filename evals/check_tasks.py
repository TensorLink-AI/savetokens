"""Grader sanity check: every task fails untouched and passes with a reference solution."""
import re
import subprocess
import tempfile
from pathlib import Path

from tasks import TASKS, materialise


def sub(ws, rel, old, new):
    p = ws / rel
    p.write_text(p.read_text().replace(old, new))


def run(ws, *cmd):
    subprocess.run(list(cmd), cwd=ws, capture_output=True)


SOLVE = {
    "off_by_one": lambda ws: sub(ws, "stats.py", "len(xs) - n)", "len(xs) - n + 1)"),
    "cli_flag": lambda ws: (ws / "wc.py").write_text(
        "import sys\n\n\ndef main(argv):\n    path = argv[-1]\n    text = open(path).read()\n"
        "    print(len(text.split()) if '--words' in argv else text.count('\\n'))\n\n\nmain(sys.argv[1:])\n"),
    "rename": lambda ws: [p.write_text(p.read_text().replace("get_user", "fetch_user")) for p in ws.rglob("*.py")],
    "big_log": lambda ws: sub(ws, "settings.ini", "timeout = 0", "timeout = 30"),
    "noisy_tests": lambda ws: sub(ws, "price.py", "round(s + tax, 2)", "round(s * (1 + tax), 2)"),
    "wide_search": lambda ws: sub(ws, "pkg/mod117.py", "RETRY_LIMIT = 0", "RETRY_LIMIT = 3"),
    "flaky_server": lambda ws: [run(ws, "python3", "-m", "pytest", "-q") for _ in range(4)],
    "polling_build": lambda ws: [run(ws, "./build.sh", "start")] + [run(ws, "./build.sh", "status") for _ in range(5)]
    + [(ws / "VERSION").write_text((ws / "out/version.txt").read_text())],
    "error_on_top": lambda ws: sub(ws, "app.cfg", "mode = legacy", "mode = modern"),
    "full_suite": lambda ws: sub(ws, "money.py", "int(x * 100)", "int(round(x * 100))"),
}

ok = True
for t in TASKS:
    for solved in (False, True):
        with tempfile.TemporaryDirectory() as d:
            ws = Path(d)
            materialise(t, ws)
            if solved:
                SOLVE[t.id](ws)
            passed, note = t.grade(ws)
            good = passed == solved
            ok &= good
            print(f"{'ok ' if good else 'BAD'} {t.id:14} {'solved' if solved else 'as-is '} -> {passed}  {note[:70]!r}")
print("all graders behave" if ok else "SOME GRADERS ARE WRONG")
