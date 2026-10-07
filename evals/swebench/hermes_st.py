"""Harbor's Hermes agent, plus a fallback model and (optionally) savetokens in the container.

Used by evals/swebench as the agent import path `hermes_st:HermesST`. Two options (agent
kwargs in the job config):

  extra_config   path on the host to a YAML fragment appended to Hermes's config.yaml
                 inside the container, e.g. the fallback provider. It must not contain
                 API keys: reference them by environment variable.
  savetokens     true installs savetokens from git in the container (during agent setup),
                 sets it up right before the run (mode lean, Hermes levers allowed), and
                 copies its lever status and database to the trial's agent logs afterwards.

Everything else (install, config, run, token counts, trajectory) is Harbor's own Hermes agent.
"""
from __future__ import annotations

import os
import shlex
from pathlib import Path

from pydantic import Field

from harbor.agents.installed.hermes import Hermes, HermesOptions

SAVETOKENS_REF = os.environ.get("SAVETOKENS_REF", "git+https://github.com/TensorLink-AI/savetokens@hermes-test")
ENV = 'export PATH="$HOME/.local/bin:$PATH" HERMES_HOME=/tmp/hermes; '
SETUP = ("{ " + ENV + "savetokens install hermes --yes --no-restart --no-ephemeris && "
         "savetokens levers on --allow hermes:side-tasks,hermes:quality-score,hermes:compaction && "
         "savetokens mode lean && savetokens maintain && savetokens levers; echo exit=$?; } "
         "> /logs/agent/savetokens-setup.txt 2>&1")
AFTER = (ENV + "savetokens levers > /logs/agent/savetokens-levers.txt 2>&1; "
         "cp ~/.savetokens/events.db /logs/agent/savetokens-events.db 2>/dev/null; true")


class HermesSTOptions(HermesOptions):
    extra_config: str | None = Field(default=None, description="Host path of a YAML fragment for config.yaml.")
    savetokens: bool = Field(default=False, description="Install and use savetokens (mode lean).")


class HermesST(Hermes):
    options_model = HermesSTOptions
    options: HermesSTOptions

    @staticmethod
    def name() -> str:
        return "hermes-st"

    async def install(self, environment) -> None:
        await super().install(environment)
        if self.options.savetokens:
            ref = shlex.quote(SAVETOKENS_REF)
            await self.exec_as_agent(environment, command=(
                ENV + f"(uv tool install -q {ref} || python3 -m pip install -q --user {ref}) && savetokens --version"))

    def _build_register_skills_command(self) -> str | None:
        # Harbor runs this right after writing config.yaml: append the fallback fragment.
        parts = [super()._build_register_skills_command()]
        if self.options.extra_config:
            fragment = Path(self.options.extra_config).expanduser().read_text().rstrip()
            parts.append(f"cat >> /tmp/hermes/config.yaml << 'STEOF'\n{fragment}\nSTEOF")
        parts = [p for p in parts if p]
        return " && ".join(f"( {p} )" for p in parts) or None

    async def exec_as_agent(self, environment, command, *args, **kwargs):
        # savetokens edits Hermes's config, so it is set up after Harbor writes config.yaml:
        # right before the `hermes chat` command, with its own timeout.
        if self.options.savetokens and "hermes --yolo chat" in command:
            await super().exec_as_agent(environment, command=SETUP, env={"HERMES_HOME": "/tmp/hermes"},
                                        timeout_sec=300)
            try:
                return await super().exec_as_agent(environment, command, *args, **kwargs)
            finally:
                try:
                    await super().exec_as_agent(environment, command=AFTER, env={"HERMES_HOME": "/tmp/hermes"},
                                                timeout_sec=60)
                except Exception:
                    pass
        return await super().exec_as_agent(environment, command, *args, **kwargs)
