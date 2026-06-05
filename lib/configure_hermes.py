#!/usr/bin/env python3
"""Idempotently wire (and un-wire) the Headroom proxy + MCP server into Hermes.

Hermes reads ``~/.hermes/config.yaml``. This helper:
  * adds a *named custom provider* pointing at the local Headroom proxy and
    selects it as the active model, and
  * (optionally, with ``--with-mcp``) registers Headroom's MCP server so the
    agent can call ``headroom_retrieve`` / ``headroom_stats`` to pull back the
    original, pre-compression content on demand.

It snapshots whatever was there before so everything can be restored when you
quit Hermes.

Run it through uv so ruamel.yaml is available without a manual install::

    uv run --with ruamel.yaml -- python lib/configure_hermes.py apply ...

Modes:
  apply    Back up config, snapshot the current ``model:`` block (and, with
           --with-mcp, the current ``mcp_servers.headroom`` entry), add/refresh
           the custom provider + MCP server, and point the active model at the
           proxy.
  restore  Put the snapshotted pieces back. Used automatically when you quit.
  remove   Delete the headroom custom provider and MCP entry entirely.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from datetime import datetime
from pathlib import Path

from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap, CommentedSeq

# Round-trip loader: preserves comments and quoting in the user's config.
yaml = YAML()
yaml.preserve_quotes = True
yaml.indent(mapping=2, sequence=4, offset=2)

# Sentinel meaning "this key did not exist before we touched it".
_ABSENT = "__absent__"


def load_config(path: Path) -> CommentedMap:
    """Load the YAML config, returning an empty map if the file is absent."""
    if not path.exists():
        return CommentedMap()
    with path.open("r", encoding="utf-8") as fh:
        data = yaml.load(fh)
    return data if isinstance(data, CommentedMap) else CommentedMap()


def save_config(path: Path, data: CommentedMap) -> None:
    """Write the YAML config back to disk, creating parent dirs as needed."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        yaml.dump(data, fh)


def backup_config(path: Path) -> Path | None:
    """Make a timestamped backup of the config before mutating it."""
    if not path.exists():
        return None
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    dest = path.with_name(f"{path.name}.bak-pre-headroom-{stamp}")
    shutil.copy2(path, dest)
    return dest


# ── custom_providers helpers ──────────────────────────────────────────────────

def get_providers(cfg: CommentedMap) -> CommentedSeq:
    """Return the ``custom_providers`` list, creating it if missing."""
    providers = cfg.get("custom_providers")
    if not isinstance(providers, list):
        providers = CommentedSeq()
        cfg["custom_providers"] = providers
    return providers


def upsert_provider(cfg: CommentedMap, name: str, base_url: str) -> None:
    """Add or update the named custom provider that points at Headroom."""
    providers = get_providers(cfg)
    for entry in providers:
        if isinstance(entry, dict) and entry.get("name") == name:
            entry["base_url"] = base_url
            entry["api_mode"] = "chat_completions"
            return
    block = CommentedMap()
    block["name"] = name
    block["base_url"] = base_url
    block["api_mode"] = "chat_completions"
    # No api_key: Hermes treats a keyless local endpoint as "no-key-required".
    # The real OpenRouter key lives in the proxy's environment, not here.
    providers.append(block)


def remove_provider(cfg: CommentedMap, name: str) -> bool:
    """Delete the named custom provider. Returns True if something was removed."""
    providers = cfg.get("custom_providers")
    if not isinstance(providers, list):
        return False
    keep = [e for e in providers
            if not (isinstance(e, dict) and e.get("name") == name)]
    if len(keep) == len(providers):
        return False
    new_seq = CommentedSeq()
    new_seq.extend(keep)
    if keep:
        cfg["custom_providers"] = new_seq
    else:  # drop the now-empty key for a clean file
        cfg.pop("custom_providers", None)
    return True


# ── mcp_servers helpers ───────────────────────────────────────────────────────

def get_mcp_servers(cfg: CommentedMap) -> CommentedMap:
    """Return the ``mcp_servers`` mapping, creating it if missing."""
    servers = cfg.get("mcp_servers")
    if not isinstance(servers, dict):
        servers = CommentedMap()
        cfg["mcp_servers"] = servers
    return servers


def upsert_mcp(cfg: CommentedMap, name: str, command: str,
               args: list[str], include_tools: list[str]) -> None:
    """Add or replace the Headroom MCP server entry (stdio transport)."""
    servers = get_mcp_servers(cfg)
    entry = CommentedMap()
    entry["command"] = command
    arg_seq = CommentedSeq()
    arg_seq.extend(args)
    entry["args"] = arg_seq
    inc = CommentedSeq()
    inc.extend(include_tools)
    tools = CommentedMap()
    tools["include"] = inc
    entry["tools"] = tools
    servers[name] = entry


def remove_mcp(cfg: CommentedMap, name: str) -> bool:
    """Delete the named MCP server entry. Returns True if removed."""
    servers = cfg.get("mcp_servers")
    if isinstance(servers, dict) and name in servers:
        del servers[name]
        if not servers:  # drop the now-empty mcp_servers key for a clean file
            cfg.pop("mcp_servers", None)
        return True
    return False


# ── commands ──────────────────────────────────────────────────────────────────

def cmd_apply(args: argparse.Namespace) -> int:
    """Point Hermes at the proxy (and MCP) and remember the prior state."""
    cfg_path = Path(args.config).expanduser()
    state_path = Path(args.state_file).expanduser()
    cfg = load_config(cfg_path)

    backup = backup_config(cfg_path)

    # Snapshot prior state (values only; comments are dropped in the snapshot).
    # Guard: if we're already wired and a snapshot exists, keep it — otherwise
    # re-running the installer would record Headroom itself as the "previous"
    # state and uninstall could never revert.
    current_model = cfg.get("model")
    already_wired = (isinstance(current_model, dict)
                     and current_model.get("provider") == f"custom:{args.provider_name}")
    if already_wired and state_path.exists():
        print("[configure_hermes] already wired; keeping existing snapshot")
    else:
        snapshot: dict[str, object] = {
            "model": None if current_model is None
            else json.loads(json.dumps(current_model)),
            "mcp_managed": bool(args.with_mcp),
        }
        if args.with_mcp:
            servers = cfg.get("mcp_servers")
            prior = (servers.get(args.provider_name)
                     if isinstance(servers, dict) else None)
            snapshot["mcp_headroom"] = (_ABSENT if prior is None
                                        else json.loads(json.dumps(prior)))
        state_path.parent.mkdir(parents=True, exist_ok=True)
        state_path.write_text(json.dumps(snapshot, indent=2), encoding="utf-8")

    # Provider definition + active model selection.
    upsert_provider(cfg, args.provider_name, args.base_url)
    model_block = CommentedMap()
    model_block["provider"] = f"custom:{args.provider_name}"
    model_block["default"] = args.model
    model_block["context_length"] = int(args.context_length)
    cfg["model"] = model_block

    # Optional MCP server so the agent can retrieve originals on demand.
    if args.with_mcp:
        mcp_args = ["mcp", "serve", "--proxy-url", args.proxy_url]
        upsert_mcp(cfg, args.provider_name, args.headroom_bin, mcp_args,
                   ["headroom_retrieve", "headroom_stats"])

    save_config(cfg_path, cfg)
    if backup is not None:
        print(f"[configure_hermes] backup: {backup}")
    print(f"[configure_hermes] active model -> "
          f"custom:{args.provider_name} / {args.model}")
    if args.with_mcp:
        print(f"[configure_hermes] MCP server '{args.provider_name}' wired "
              f"(headroom_retrieve, headroom_stats)")
    return 0


def cmd_restore(args: argparse.Namespace) -> int:
    """Undo apply: restore the model block and the MCP entry we changed."""
    cfg_path = Path(args.config).expanduser()
    state_path = Path(args.state_file).expanduser()
    cfg = load_config(cfg_path)

    if not state_path.exists():
        print("[configure_hermes] no saved state; nothing to restore")
        return 0
    snapshot = json.loads(state_path.read_text(encoding="utf-8"))

    # Restore the model selection only if Headroom is still active, so a
    # mid-session /model switch by the user is respected.
    active = cfg.get("model", {})
    active_provider = active.get("provider") if isinstance(active, dict) else None
    if active_provider == f"custom:{args.provider_name}":
        prior_model = snapshot.get("model")
        if prior_model is None:
            cfg.pop("model", None)
        else:
            cfg["model"] = prior_model
        print("[configure_hermes] restored previous model selection")
    else:
        print("[configure_hermes] active provider changed; leaving model as-is")

    # Restore the MCP entry if we were the ones managing it.
    if snapshot.get("mcp_managed"):
        prior_mcp = snapshot.get("mcp_headroom", _ABSENT)
        if prior_mcp == _ABSENT:
            remove_mcp(cfg, args.provider_name)
        else:
            get_mcp_servers(cfg)[args.provider_name] = prior_mcp
        print("[configure_hermes] restored previous MCP state")

    save_config(cfg_path, cfg)
    return 0


def cmd_remove(args: argparse.Namespace) -> int:
    """Delete the headroom custom provider and MCP entry entirely."""
    cfg_path = Path(args.config).expanduser()
    cfg = load_config(cfg_path)
    changed = remove_provider(cfg, args.provider_name)
    changed = remove_mcp(cfg, args.provider_name) or changed
    if changed:
        save_config(cfg_path, cfg)
        print("[configure_hermes] removed headroom provider + MCP entry")
    else:
        print("[configure_hermes] nothing to remove")
    return 0


# ── CLI ───────────────────────────────────────────────────────────────────────

def build_parser() -> argparse.ArgumentParser:
    """Construct the CLI: shared options plus apply/restore/remove subcommands."""
    parser = argparse.ArgumentParser(description="Wire Headroom into Hermes config.")
    parser.add_argument("--config", default="~/.hermes/config.yaml",
                        help="Path to Hermes config (default: ~/.hermes/config.yaml)")
    parser.add_argument("--state-file", default="./.runtime/model_state.json",
                        help="Where to store the pre-wrap snapshot")
    parser.add_argument("--provider-name", default="headroom",
                        help="custom_providers / mcp_servers name (default: headroom)")
    sub = parser.add_subparsers(dest="command", required=True)

    p_apply = sub.add_parser("apply", help="point Hermes at the Headroom proxy")
    p_apply.add_argument("--base-url", required=True)
    p_apply.add_argument("--model", required=True)
    p_apply.add_argument("--context-length", default="128000")
    p_apply.add_argument("--with-mcp", action="store_true",
                         help="also register Headroom's MCP server")
    p_apply.add_argument("--proxy-url", default="http://127.0.0.1:8787",
                         help="proxy base URL the MCP server connects to")
    p_apply.add_argument("--headroom-bin", default="headroom",
                         help="path to the headroom executable for MCP stdio")
    p_apply.set_defaults(func=cmd_apply)

    sub.add_parser("restore", help="undo apply if Headroom is still active"
                   ).set_defaults(func=cmd_restore)
    sub.add_parser("remove", help="delete the headroom provider + MCP entry"
                   ).set_defaults(func=cmd_remove)
    return parser


def main(argv: list[str] | None = None) -> int:
    """Parse arguments and dispatch to the selected subcommand."""
    args = build_parser().parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
