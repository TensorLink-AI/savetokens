# Setups

Recipes for common setups. Each one starts from [Getting started](getting-started.md) step 1 (install), and
mixes freely with the others.

- [Claude Code or Codex on a subscription](#claude-code-or-codex-on-a-subscription)
- [Several subscriptions (accounts) on one tool](#several-subscriptions-accounts-on-one-tool)
- [Claude Code or Codex on an API key](#claude-code-or-codex-on-an-api-key)
- [Hermes on any pay-as-you-go provider](#hermes-on-any-pay-as-you-go-provider)
- [Several machines](#several-machines)
- [CI, containers and GPU pods](#ci-containers-and-gpu-pods)
- [Several projects on one machine](#several-projects-on-one-machine)
- [One project across machines and accounts](#one-project-across-machines-and-accounts)
- [A team](#a-team)
- [Nothing leaves this machine](#nothing-leaves-this-machine)

## Claude Code or Codex on a subscription

Nothing to do after install. savetokens reads each tool's own meter (the 5-hour and weekly limits, and when
each resets). That meter counts everything you do on the account: every machine, the web app, every session.
Setup reads your plan's name from your login and offers its list price, which is only used to work out what
each limit's worth in dollars.

```sh
savetokens plan anthropic --usd 200 --per month   # Claude Max 20x, if setup didn't catch it
savetokens plan openai --usd 200 --per month      # ChatGPT Pro
```

## Several subscriptions (accounts) on one tool

Sign in to each account in the usual way (`/login` in Claude Code, `codex login` in Codex). savetokens tags
every reading with the account it came from, stored as a short hash: no names or emails. Once two or more
accounts on the same tool have been used in the last 8 days, each gets its own limits, forecast and alerts. The
dashboard labels them "Claude Code (account 2)" and so on. With one account, nothing changes.

Same account on several machines? Join them all to one hub (see [Several machines](#several-machines)), so the
readings add up.

Separate config folders: Claude Code with `CLAUDE_CONFIG_DIR` works if you run
`CLAUDE_CONFIG_DIR=... savetokens install` once for each folder, so each one gets the statusline. The
background reader only reads the default `~/.codex` and `~/.hermes` (and every Hermes profile under it). For
Codex, switch accounts by signing in again rather than keeping a second `CODEX_HOME`.

## Claude Code or Codex on an API key

Detected on its own: Claude Code on a key sends no plan meter, and Codex reports when it's logged in with a key.
Setup then asks for a budget. To set or change it yourself:

```sh
savetokens budget --provider anthropic --usd 200 --per month
savetokens budget --tool codex --usd 50 --per week
savetokens price gpt-6-astra 2 8      # a model savetokens can't price: $ per million tokens in, out
```

Prices come from a catalogue of every model OpenRouter lists, refreshed daily, so new models are usually
priced already.

## Hermes on any pay-as-you-go provider

Hermes is always pay as you go, on whichever provider you point it at: OpenRouter, Anthropic, OpenAI, Nous,
Chutes, a local server. savetokens reads `~/.hermes/state.db` and every profile's
(`~/.hermes/profiles/*/state.db`). Usage shows up under the provider's own name. Give each provider you pay
its own budget:

```sh
savetokens spend                                              # which providers you've used, and what they cost
savetokens budget --provider chutes --usd 20 --per month      # a provider by any of its names (chutes, chutes.ai)
savetokens budget --provider openrouter --usd 100 --per month
savetokens plan synthetic.new --usd 20 --per month            # a flat-rate provider: a subscription, not a budget
```

If Hermes reports no cost (some providers don't), savetokens prices the tokens from its catalogue. If the
catalogue doesn't know the model either, run `savetokens price MODEL IN OUT`.

## Several machines

Limits belong to the account, so put every machine on one hub:

```sh
savetokens server --setup                         # on the always-on machine: prints the join line
curl -fsSL https://your-hub/join | sh -s CODE     # on every other machine
savetokens pair --machines 5 --hours 24           # more codes later, from any joined machine
```

- **Reaching the hub:** Tailscale on every machine is the easy way: free, private and HTTPS. Otherwise open
  port 8787 on the hub, or put it behind your own domain with
  `savetokens server --setup --public-url https://hub.example.com`.
- **No always-on machine:** deploy the hub to Render with one button, or anywhere with Docker
  (`docker compose up -d`). See the README's [Run a hub](../README.md#run-a-hub-all-your-machines-in-one-place).
- **A hub on WSL:** other machines can't reach WSL's address. Install Tailscale inside WSL, or turn on
  mirrored networking.

Every machine's budgets, projects and alerts then cover all of them. The dashboard is at the hub's address.

## CI, containers and GPU pods

No install. In the dashboard, go to **Add a machine** → **Make a token**, then set two variables where the job runs:

```sh
export SAVETOKENS_SERVER=https://your-hub SAVETOKENS_TOKEN=st_...
```

Any other agent or app that exports OpenTelemetry can report straight to the hub: `savetokens otel` prints
the lines to paste.

## Several projects on one machine

Nothing to do. Each git repository is its own project, and each non-default branch is a task inside it.

```sh
savetokens projects                               # every project: tokens, API value, API spend, by tool and provider
savetokens budget --project webapp --usd 150 --per month
savetokens task start "Add OAuth"                 # or declare a task yourself
```

A folder that isn't a git repo is a project on this machine only. To make it (and every repo under it) one
project, run `savetokens project link NAME` there.

## One project across machines and accounts

A git repository is the same project on every machine joined to your hub, however it was cloned, and whichever
account or tool works on it. For a folder without git, run `savetokens project link NAME` once and commit (or
copy) the `.savetokens` file it writes.

Only an id goes to the hub. To see names on the hub's dashboard, run `savetokens projects --share-names on` on
the machine that knows them.

## A team

```sh
savetokens server --team acme --member sam --role admin     # on the hub: sam gets a join line
savetokens team                                             # anyone: totals, projects, budgets
savetokens budget --team --project webapp --usd 2000 --per month
savetokens project share                                    # the team sees this project's name
```

## Nothing leaves this machine

```sh
savetokens install --no-ephemeris
```

Forecasts are made locally, and there's no hub. What leaves in the other setups is listed in the README:
[What leaves your machine](../README.md#what-leaves-your-machine).
