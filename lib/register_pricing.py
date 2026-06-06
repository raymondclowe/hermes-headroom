#!/usr/bin/env python3
"""Register OpenRouter model prices into LiteLLM's cost map.

Headroom's `headroom perf` shows dollar savings by looking up
``litellm.model_cost[<exact model slug>]``. LiteLLM ships a bundled price
database, but brand-new models (e.g. ``deepseek/deepseek-v4-flash`` / ``-pro``)
are not in it yet — not even upstream — so the report prints "list price
unknown" and omits per-token / context info.

LiteLLM exposes no environment hook for *additive* custom pricing, and the
Headroom proxy never calls ``litellm.register_model`` itself, so the reliable
fix is to add the entries to LiteLLM's bundled cost-map JSON inside Headroom's
own venv. Prices are pulled live from OpenRouter, so they stay correct and the
tool is safe to re-run (e.g. after ``uv tool upgrade headroom-ai`` overwrites
the file). Every entry we add is tagged so ``--restore`` can remove exactly
ours without touching LiteLLM's curated data.

This does NOT affect a running proxy (it already loaded its cost map at
startup); `headroom perf` is a fresh process and picks up the prices at once.
"""

from __future__ import annotations

import argparse
import glob
import json
import shutil
import subprocess
import sys
import tempfile
import urllib.request
from pathlib import Path

MARKER = "hermes-headroom"  # tags entries we own, for clean removal
OPENROUTER_MODELS_URL = "https://openrouter.ai/api/v1/models"
DEFAULT_SLUGS = ["deepseek/deepseek-v4-flash", "deepseek/deepseek-v4-pro"]


def find_litellm_costmap() -> Path:
    """Locate the cost-map JSON inside the `headroom` tool's venv."""
    hb = shutil.which("headroom")
    if not hb:
        raise SystemExit("[register_pricing] 'headroom' not on PATH")
    venv_root = Path(hb).resolve().parents[1]  # .../bin/headroom -> venv root
    matches = glob.glob(
        str(venv_root / "lib" / "python*" / "site-packages" / "litellm"
            / "model_prices_and_context_window_backup.json")
    )
    if not matches:
        raise SystemExit(
            "[register_pricing] could not find LiteLLM cost map under "
            f"{venv_root}")
    return Path(matches[0])


def fetch_openrouter_pricing(slugs: list[str]) -> dict[str, dict]:
    """Fetch current OpenRouter pricing/context for the given model slugs."""
    req = urllib.request.Request(
        OPENROUTER_MODELS_URL, headers={"User-Agent": "hermes-headroom"})
    with urllib.request.urlopen(req, timeout=30) as resp:  # noqa: S310
        catalog = json.loads(resp.read().decode("utf-8")).get("data", [])

    by_id = {m.get("id"): m for m in catalog}
    out: dict[str, dict] = {}
    for slug in slugs:
        m = by_id.get(slug)
        if m is None:
            print(f"[register_pricing] WARNING: {slug} not found on OpenRouter")
            continue
        pricing = m.get("pricing", {})
        prompt = float(pricing.get("prompt", 0) or 0)
        completion = float(pricing.get("completion", 0) or 0)
        cache_read = float(pricing.get("input_cache_read", 0) or 0)
        cache_write = float(pricing.get("input_cache_write", 0) or 0)
        ctx = int(m.get("context_length") or 0)

        entry: dict[str, object] = {
            "litellm_provider": "openrouter",
            "mode": "chat",
            "input_cost_per_token": prompt,
            "output_cost_per_token": completion,
            # DeepSeek bills cache writes at the input rate unless OpenRouter
            # reports a distinct write price.
            "cache_creation_input_token_cost": cache_write or prompt,
            "_source": MARKER,
        }
        if cache_read:
            entry["cache_read_input_token_cost"] = cache_read
        if ctx:
            entry["max_tokens"] = ctx
            entry["max_input_tokens"] = ctx
            entry["max_output_tokens"] = ctx
        out[slug] = entry
    return out


def load_json(path: Path) -> dict:
    """Parse the cost-map JSON, tolerating a leading comment key if present."""
    return json.loads(path.read_text(encoding="utf-8"))


def atomic_write(path: Path, data: dict) -> None:
    """Write JSON atomically so a crash can never leave a half-written map."""
    with tempfile.NamedTemporaryFile(
        "w", dir=str(path.parent), delete=False, encoding="utf-8") as tmp:
        json.dump(data, tmp, indent=4)
        tmp_path = Path(tmp.name)
    json.loads(tmp_path.read_text(encoding="utf-8"))  # validate before swap
    tmp_path.replace(path)


def verify_with_headroom_venv(slugs: list[str]) -> bool:
    """Confirm the headroom venv's litellm now resolves the slugs."""
    hb = shutil.which("headroom")
    py = Path(hb).resolve().parents[1] / "bin" / "python"
    code = (
        "import litellm, json, sys;"
        "mc = litellm.model_cost;"
        f"print(json.dumps({{s: (s in mc) for s in {slugs!r}}}))"
    )
    try:
        res = subprocess.run([str(py), "-c", code], capture_output=True,
                             text=True, timeout=60, check=True)
        return all(json.loads(res.stdout.strip()).values())
    except Exception as exc:  # noqa: BLE001
        print(f"[register_pricing] verify failed: {exc}")
        return False


def cmd_apply(slugs: list[str]) -> int:
    """Add/refresh our price entries in the LiteLLM cost map."""
    costmap = find_litellm_costmap()
    backup = costmap.with_suffix(costmap.suffix + ".hh-backup")
    if not backup.exists():
        shutil.copy2(costmap, backup)
        print(f"[register_pricing] backed up cost map -> {backup.name}")

    prices = fetch_openrouter_pricing(slugs)
    if not prices:
        print("[register_pricing] nothing to register")
        return 1

    data = load_json(costmap)
    for slug, entry in prices.items():
        existing = data.get(slug)
        if existing and existing.get("_source") != MARKER:
            print(f"[register_pricing] skip {slug}: already curated by LiteLLM")
            continue
        data[slug] = entry
        ppm = entry["input_cost_per_token"] * 1_000_000
        print(f"[register_pricing] registered {slug}  (${ppm:.4f}/M in)")

    atomic_write(costmap, data)
    ok = verify_with_headroom_venv(list(prices.keys()))
    print("[register_pricing] verified in headroom venv"
          if ok else "[register_pricing] WARNING: verification did not confirm")
    return 0 if ok else 2


def cmd_restore() -> int:
    """Remove only the entries we added (identified by their marker)."""
    costmap = find_litellm_costmap()
    data = load_json(costmap)
    removed = [k for k, v in list(data.items())
               if isinstance(v, dict) and v.get("_source") == MARKER]
    for k in removed:
        del data[k]
    if removed:
        atomic_write(costmap, data)
        print(f"[register_pricing] removed {len(removed)} entry(ies): "
              f"{', '.join(removed)}")
    else:
        print("[register_pricing] no hermes-headroom entries to remove")
    return 0


def main(argv: list[str] | None = None) -> int:
    """Parse arguments and dispatch apply/restore."""
    parser = argparse.ArgumentParser(
        description="Register OpenRouter model prices into LiteLLM's cost map.")
    parser.add_argument("--slugs", default=",".join(DEFAULT_SLUGS),
                        help="comma-separated OpenRouter model ids to register")
    parser.add_argument("--restore", action="store_true",
                        help="remove the entries this tool added")
    args = parser.parse_args(argv)

    if args.restore:
        return cmd_restore()
    slugs = [s.strip() for s in args.slugs.split(",") if s.strip()]
    return cmd_apply(slugs)


if __name__ == "__main__":
    sys.exit(main())
