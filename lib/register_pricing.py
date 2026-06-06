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
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.request
from pathlib import Path

MARKER = "hermes-headroom"  # tags entries we own, for clean removal
OPENROUTER_MODELS_URL = "https://openrouter.ai/api/v1/models"
DEFAULT_SLUGS = ["deepseek/deepseek-v4-flash", "deepseek/deepseek-v4-pro"]
DEFAULT_LOG_DIR = Path.home() / ".headroom" / "logs"
# Headroom's native per-model context/pricing registry (read by the OpenAI-
# compatible provider the OpenRouter backend uses). LiteLLM fixes `perf` $
# display, but the *context limit* the proxy uses for trimming comes from here —
# unknown models otherwise fall back to a wrong 128K default.
MODELS_JSON = Path.home() / ".headroom" / "models.json"


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


def fetch_catalog() -> dict[str, dict]:
    """Fetch the OpenRouter model catalog as a {slug: model_obj} mapping."""
    req = urllib.request.Request(
        OPENROUTER_MODELS_URL, headers={"User-Agent": "hermes-headroom"})
    with urllib.request.urlopen(req, timeout=30) as resp:  # noqa: S310
        catalog = json.loads(resp.read().decode("utf-8")).get("data", [])
    return {m.get("id"): m for m in catalog if m.get("id")}


def build_entry(model_obj: dict) -> dict:
    """Build a LiteLLM cost-map entry from one OpenRouter model object."""
    pricing = model_obj.get("pricing", {})
    prompt = float(pricing.get("prompt", 0) or 0)
    completion = float(pricing.get("completion", 0) or 0)
    cache_read = float(pricing.get("input_cache_read", 0) or 0)
    cache_write = float(pricing.get("input_cache_write", 0) or 0)
    ctx = int(model_obj.get("context_length") or 0)

    entry: dict[str, object] = {
        "litellm_provider": "openrouter",
        "mode": "chat",
        "input_cost_per_token": prompt,
        "output_cost_per_token": completion,
        # Providers that don't report a distinct cache-write price bill it at
        # the input rate.
        "cache_creation_input_token_cost": cache_write or prompt,
        "_source": MARKER,
    }
    if cache_read:
        entry["cache_read_input_token_cost"] = cache_read
    if ctx:
        entry["max_tokens"] = ctx
        entry["max_input_tokens"] = ctx
        entry["max_output_tokens"] = ctx
    return entry


def discover_slugs_from_logs(log_dir: Path) -> list[str]:
    """Scan Headroom's proxy logs for provider/model slugs actually used.

    Lets the tool track whatever models flow through the proxy (profiles and
    models change often) instead of relying on a hardcoded list. Candidates are
    validated against the live OpenRouter catalog by the caller, so a loose
    match here is harmless.
    """
    if not log_dir.is_dir():
        return []
    slug_re = re.compile(r"\b[a-z0-9-]+/[A-Za-z0-9][A-Za-z0-9._-]*\b")
    found: set[str] = set()
    for log in log_dir.glob("*.log"):
        try:
            text = log.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for m in slug_re.findall(text):
            # exclude obvious non-models (paths, the /models probe label)
            if m.startswith(("/", "headroom", "home/")) or ":" in m:
                continue
            found.add(m)
    return sorted(found)


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


def cmd_apply(slugs: list[str], from_logs: bool, log_dir: Path) -> int:
    """Add/refresh price entries in the LiteLLM cost map.

    Registers the explicit ``slugs`` (warning if a requested one is unknown)
    plus, when ``from_logs`` is set, every model seen in the proxy logs that
    exists in the OpenRouter catalog.
    """
    costmap = find_litellm_costmap()
    backup = costmap.with_suffix(costmap.suffix + ".hh-backup")
    if not backup.exists():
        shutil.copy2(costmap, backup)
        print(f"[register_pricing] backed up cost map -> {backup.name}")

    catalog = fetch_catalog()

    targets: dict[str, dict] = {}
    for slug in slugs:  # explicit: warn if not on OpenRouter
        if slug in catalog:
            targets[slug] = build_entry(catalog[slug])
        else:
            print(f"[register_pricing] WARNING: {slug} not found on OpenRouter")
    if from_logs:  # discovered: silently keep only real catalog models
        discovered = [s for s in discover_slugs_from_logs(log_dir)
                      if s in catalog]
        for slug in discovered:
            targets.setdefault(slug, build_entry(catalog[slug]))
        print(f"[register_pricing] discovered {len(discovered)} model(s) in "
              f"logs: {', '.join(discovered) or '(none)'}")

    if not targets:
        print("[register_pricing] nothing to register")
        return 1

    data = load_json(costmap)
    for slug, entry in targets.items():
        existing = data.get(slug)
        if existing and existing.get("_source") != MARKER:
            print(f"[register_pricing] skip {slug}: already curated by LiteLLM")
            continue
        data[slug] = entry
        ppm = entry["input_cost_per_token"] * 1_000_000
        print(f"[register_pricing] registered {slug}  (${ppm:.4f}/M in)")

    atomic_write(costmap, data)
    ok = verify_with_headroom_venv(list(targets.keys()))
    print("[register_pricing] verified in headroom venv"
          if ok else "[register_pricing] WARNING: verification did not confirm")
    # Also set native context limits so the proxy stops assuming a 128K window.
    update_models_json(list(targets.keys()), catalog)
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
    restore_models_json()
    return 0


def update_models_json(slugs: list[str], catalog: dict[str, dict]) -> None:
    """Write context limits (+ pricing) for `slugs` into ~/.headroom/models.json.

    Uses the OpenAI-compatible provider's ``openai`` section (the OpenRouter
    backend's provider type). Tracks which keys we added under a private marker
    so ``restore`` removes only ours and never the user's own entries.
    """
    if not slugs:
        return
    data: dict = {}
    if MODELS_JSON.exists():
        try:
            data = json.loads(MODELS_JSON.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            data = {}

    section = data.setdefault("openai", {})
    ctx_limits = section.setdefault("context_limits", {})
    pricing = section.setdefault("pricing", {})
    managed = set(data.setdefault(f"_{MARKER}", {}).setdefault("managed", []))

    written = 0
    for slug in slugs:
        m = catalog.get(slug)
        if not m:
            continue
        ctx = int(m.get("context_length") or 0)
        if not ctx:
            continue
        # only manage keys we created; never clobber user-defined ones
        if slug in ctx_limits and slug not in managed:
            continue
        ctx_limits[slug] = ctx
        p = m.get("pricing", {})
        pricing[slug] = {  # per-million USD, matching models.json schema
            "input": round(float(p.get("prompt", 0) or 0) * 1_000_000, 6),
            "output": round(float(p.get("completion", 0) or 0) * 1_000_000, 6),
            "cached_input": round(
                float(p.get("input_cache_read", 0) or 0) * 1_000_000, 6),
        }
        managed.add(slug)
        written += 1

    data[f"_{MARKER}"]["managed"] = sorted(managed)
    MODELS_JSON.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w", dir=str(MODELS_JSON.parent), delete=False, encoding="utf-8") as tmp:
        json.dump(data, tmp, indent=2)
        tmp_path = Path(tmp.name)
    json.loads(tmp_path.read_text(encoding="utf-8"))  # validate
    tmp_path.replace(MODELS_JSON)
    print(f"[register_pricing] models.json: set context limits for {written} "
          f"model(s) (fixes the 128K fallback)")


def restore_models_json() -> None:
    """Remove only the context/pricing entries we added to models.json."""
    if not MODELS_JSON.exists():
        return
    try:
        data = json.loads(MODELS_JSON.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return
    managed = data.get(f"_{MARKER}", {}).get("managed", [])
    if not managed:
        return
    section = data.get("openai", {})
    for slug in managed:
        section.get("context_limits", {}).pop(slug, None)
        section.get("pricing", {}).pop(slug, None)
    data.pop(f"_{MARKER}", None)
    # If our section is now empty, drop it; if the whole file is now empty, remove it.
    if not section.get("context_limits") and not section.get("pricing"):
        data.pop("openai", None)
    if data:
        MODELS_JSON.write_text(json.dumps(data, indent=2), encoding="utf-8")
    else:
        MODELS_JSON.unlink()
    print(f"[register_pricing] models.json: removed {len(managed)} entry(ies)")


def main(argv: list[str] | None = None) -> int:
    """Parse arguments and dispatch apply/restore."""
    parser = argparse.ArgumentParser(
        description="Register OpenRouter model prices into LiteLLM's cost map.")
    parser.add_argument("--slugs", default=",".join(DEFAULT_SLUGS),
                        help="comma-separated OpenRouter model ids to register")
    parser.add_argument("--from-logs", action="store_true",
                        help="also register every model found in the proxy "
                             "logs (tracks your actual traffic automatically)")
    parser.add_argument("--log-dir", default=str(DEFAULT_LOG_DIR),
                        help="proxy log directory to scan with --from-logs")
    parser.add_argument("--restore", action="store_true",
                        help="remove the entries this tool added")
    args = parser.parse_args(argv)

    if args.restore:
        return cmd_restore()
    slugs = [s.strip() for s in args.slugs.split(",") if s.strip()]
    return cmd_apply(slugs, args.from_logs, Path(args.log_dir))


if __name__ == "__main__":
    sys.exit(main())
