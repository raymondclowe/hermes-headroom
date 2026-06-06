# hermes-headroom

Put **[Headroom](https://github.com/chopratejas/headroom)** compression in front of
**[Hermes Agent](https://github.com/NousResearch/hermes-agent)** *persistently and
globally*, using **OpenRouter** as the model provider. Every tool output, log,
file and RAG chunk gets compressed before it reaches the model — typically 60–95%
fewer tokens for the same answers.

This is **not** a per-session launcher. It wires Headroom in once, so the
**gateway** (Discord, Telegram, etc.), **cron jobs**, and the plain `hermes` CLI
all route through it automatically:

```
Hermes gateway / Discord / all profiles / crons / kanban / subagents / CLI
        │  (every process reads OPENROUTER_BASE_URL from ~/.hermes/.env)
        ▼
Headroom proxy  ──►  OpenRouter  ──►  the model
(systemd --user service, always on; compresses + caches)
```

It works because Hermes reads **`OPENROUTER_BASE_URL`** from `~/.hermes/.env` and
applies it to *every* process that uses the `openrouter` provider. Setting that one
variable to the local proxy routes the gateway, all profiles, crons, kanban workers,
delegated subagents, and the CLI through Headroom at once — no `config.yaml` or
per-profile edits, and it survives profile/model changes.

---

## What you need first

1. **Hermes Agent** installed (`hermes` on your PATH), normally run as a gateway
   service (`hermes gateway install` / `start`).
2. **uv** installed: `curl -LsSf https://astral.sh/uv/install.sh | sh`
3. An **OpenRouter API key** — <https://openrouter.ai/keys>.

Headroom is installed for you by `setup.sh` (or `uv tool install "headroom-ai[proxy,mcp]"`).
The default model is **DeepSeek V4 Flash** (`deepseek/deepseek-v4-flash`) — fast,
~1M context, very cheap. Change `MODEL` in `.env` for anything else.

---

## Quick start

```bash
cd hermes-headroom
./setup.sh                 # checks tools, installs Headroom, creates .env (key + model)
./install.sh               # runs the proxy as a service + points Hermes at it, for good
hermes gateway restart     # one-time: gateway picks up the new config
```

That's it. Nothing to launch per session. Verify it's working:

```bash
systemctl --user status headroom-proxy.service   # proxy is active
headroom perf --hours 24 # tokens / cost saved (last 24 hours) No flag for last 7 days.
```

Either run LITELLM_LOCAL_MODEL_COST_MAP=true headroom perf, or set it once so plain headroom perf works:


fish:

```bash
set -Ux LITELLM_LOCAL_MODEL_COST_MAP true
```

bash/zsh:

```bash
echo 'export LITELLM_LOCAL_MODEL_COST_MAP=true' >> ~/.bashrc
```

To restart the proxy, run:

```
systemctl --user restart headroom-proxy.service
```

To change the model or port later, edit `.env` and re-run `./install.sh`.
To remove everything, `./uninstall.sh`.

---

## What `install.sh` actually changes

1. **A user service** at `~/.config/systemd/user/headroom-proxy.service` running
   `headroom proxy --backend openrouter` on `127.0.0.1:<PORT>`, enabled so it
   starts on login and auto-restarts on failure. Your `.env` is the service's
   `EnvironmentFile`, so the OpenRouter key lives there and nowhere else.
   (This `.env` deliberately does **not** set `OPENROUTER_BASE_URL`, so the proxy
   itself still targets real OpenRouter — no loop.)
2. **`~/.hermes/.env`** — a small, clearly-marked, removable block that sets
   `OPENROUTER_BASE_URL=http://127.0.0.1:<PORT>/v1`. This is the global lever:
   every Hermes process using the `openrouter` provider now flows through the
   proxy. No `config.yaml` or per-profile edits; survives profile/model changes.
   Any pre-existing `OPENROUTER_BASE_URL` is preserved and restored on uninstall.
3. **(Optional, `ENABLE_MCP=1`) the default `~/.hermes/config.yaml`** gets a
   `headroom` entry under `mcp_servers` for the retrieve tool (timestamped backup
   first). MCP servers have no global env lever, so this is added to the **default
   config only** — it is the one thing that can't be made profile-wide without
   per-profile edits, which we deliberately avoid. Compression works fully without it.
4. **A boot-ordering drop-in** at
   `~/.config/systemd/user/hermes-gateway.service.d/10-headroom.conf` so the
   gateway starts *after* the proxy. This is a separate file — it does not modify
   the unit Hermes generates, and survives `hermes gateway` regenerating it.

`uninstall.sh` reverses all of the above (and cleans up any legacy wiring from
earlier versions).

### What this does *not* catch

`OPENROUTER_BASE_URL` only wraps traffic Hermes routes **through OpenRouter**. When
Hermes shells out to **Codex** or **Claude Code** as subagents, those CLIs call
Anthropic/OpenAI on their own endpoints, so they bypass this proxy. To compress
those too, run a second Headroom proxy with `--backend anthropic` (or `openai`) and
point `ANTHROPIC_BASE_URL` / `OPENAI_BASE_URL` at it, or use `headroom wrap codex` /
`headroom wrap claude`. That's a separate, additive setup.

---

## Choosing a model

Put any OpenRouter slug in `.env` as `MODEL=`, exactly as shown on
<https://openrouter.ai/models>. Set `CONTEXT_LENGTH` to that model's real window
(Hermes needs ≥ 64000 for agent use; DeepSeek V4 Flash uses `1000000`). Re-run
`./install.sh` after changing either.

## Settings (`.env`)

| Setting | What it is |
| --- | --- |
| `OPENROUTER_API_KEY` | Your OpenRouter key. Read by the proxy service only. |
| `MODEL` | OpenRouter model slug to run. |
| `PORT` | Local port for the proxy (default `8787`). |
| `CONTEXT_LENGTH` | Context window in tokens Hermes should assume. |
| `ENABLE_MCP` | `1` wires Headroom's retrieve/stats MCP tools into Hermes; `0` skips. |
| `HEADROOM_EXTRA_ARGS` | Reserved for advanced proxy flags (not used by the service unit). |

---

## Reversible compression — the MCP retrieve tool

With `ENABLE_MCP=1` (default), the agent gets `headroom_retrieve` (pull back the
exact original bytes behind a compressed block, by hash) and `headroom_stats`,
pointed at the same always-on proxy. Inside Hermes they appear as
`mcp_headroom_headroom_retrieve` / `mcp_headroom_headroom_stats`. Proxy-backed
originals are cached ~5 minutes, so retrieval is for in-flight detail, not recall
from hours ago.

---

## Reliability — important

Because the whole agent now routes through the proxy, **if the proxy is down, all
Hermes model calls fail** (gateway included). Two things mitigate this:

- The proxy runs with `Restart=always`, so systemd brings it straight back if it
  crashes, and the gateway is ordered to start after it at boot.
- For belt-and-suspenders, add a direct-to-OpenRouter **fallback** so the bot keeps
  working (uncompressed) if the proxy is ever unreachable. This needs your key in
  `~/.hermes/.env` (`OPENROUTER_API_KEY=...`), then add to `~/.hermes/config.yaml`:

  ```yaml
  fallback_providers:
    - provider: openrouter
      model: deepseek/deepseek-v4-flash
  ```

If linger isn't enabled for your user, the proxy (and gateway) won't survive a full
logout/reboot. The Hermes gateway docs cover this — `loginctl enable-linger $USER`.
Check if set with

```bash
loginctl show-user $USER | grep Linger
ls /var/lib/systemd/linger/
```

If your gateway already survives reboots, the proxy will too.

---

## Troubleshooting

- **Proxy won't start / bot stops responding.**
  `systemctl --user status headroom-proxy.service` and
  `journalctl --user -u headroom-proxy.service -e`. Most common cause: a bad
  `OPENROUTER_API_KEY` in `.env`.
- **`install.sh` can't reach `systemctl --user`** (headless/SSH): run
  `export XDG_RUNTIME_DIR=/run/user/$(id -u)` then re-run.
- **"model not found".** The `MODEL` slug in `.env` is the thing to fix; use the
  exact slug from openrouter.ai/models, then re-run `./install.sh`.
- **Want the original config back?** Each `install.sh` leaves a timestamped backup:
  `~/.hermes/config.yaml.bak-pre-headroom-*`.
- **`headroom perf` shows "list price unknown" / no $ savings.** The model is
  newer than LiteLLM's price database (true today for `deepseek-v4-*`).
  `install.sh` fixes this by registering live OpenRouter prices into LiteLLM's
  *local* cost map (`lib/register_pricing.py`) and setting
  `LITELLM_LOCAL_MODEL_COST_MAP=true`. Re-run `./install.sh` (or
  `python3 lib/register_pricing.py --slugs <your/model>`) after
  `uv tool upgrade headroom-ai`, or for any other new model. The `perf` CLI must
  see the flag too, so run it as `LITELLM_LOCAL_MODEL_COST_MAP=true headroom perf`
  — or add `export LITELLM_LOCAL_MODEL_COST_MAP=true` to your shell rc so plain
  `headroom perf` works. (`fish`: `set -Ux LITELLM_LOCAL_MODEL_COST_MAP true`.)
- **token vs cache mode.** `HEADROOM_MODE=token` (default) maximizes token
  reduction but rewrites prior turns, which churns provider prefix-cache early in
  a conversation. `HEADROOM_MODE=cache` freezes prior turns to preserve cache
  hits — usually cheaper only for models with steep cache discounts (e.g.
  deepseek-v4-**pro**, whose cache reads are ~120× cheaper than fresh input). For
  flash-dominant traffic, token mode is typically the better bill. Switch in
  `.env`, restart the proxy, and compare `headroom perf`.


---

## License

MIT. Headroom is Apache-2.0, Hermes Agent is MIT — this project only orchestrates
them; it bundles neither.
