#!/usr/bin/env python3
"""Patch the upstream s2_event_horizon_cycle beta grid to test below 0.35.

Run from the root of the s2_event_horizon_cycle repo:

    python source_cycle_beta_patch/scripts/patch_beta_grid.py

The patch is deliberately conservative. It edits likely beta-grid definitions in
scripts/update_news.py and writes a .bak file. If it cannot find the beta grid,
it exits with instructions instead of silently changing unrelated code.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

EXPANDED = "[0.15, 0.20, 0.25, 0.30, 0.35, 0.40, 0.45, 0.55, 0.65, 0.75, 0.85, 1.00, 1.15, 1.35, 1.50, 1.75, 2.00, 2.25]"
ROOT = Path.cwd()
TARGETS = [ROOT / "scripts" / "update_news.py", ROOT / "scripts" / "fit_cycles.py"]

PATTERNS = [
    # BETA_GRID = [ ... 0.35 ... ]
    (re.compile(r"(?P<prefix>\b(?:BETA|BETA_GRID|BETA_VALUES|beta_grid|beta_values)[A-Za-z0-9_]*\s*=\s*)\[[^\]]*0\.35[^\]]*\]", re.S), r"\g<prefix>" + EXPANDED),
    # for beta in [ ... 0.35 ... ]
    (re.compile(r"(?P<prefix>for\s+beta\s+in\s*)\[[^\]]*0\.35[^\]]*\]", re.S), r"\g<prefix>" + EXPANDED),
    # beta_candidates = tuple/list(...)
    (re.compile(r"(?P<prefix>\b(?:beta_candidates|beta_grid|beta_values)\s*=\s*(?:tuple|list)\s*\()\[[^\]]*0\.35[^\]]*\](?P<suffix>\))", re.S), r"\g<prefix>" + EXPANDED + r"\g<suffix>"),
]

changed = []
examined = []
for target in TARGETS:
    if not target.exists():
        continue
    examined.append(str(target.relative_to(ROOT)))
    text = target.read_text(encoding="utf-8")
    original = text
    for pat, repl in PATTERNS:
        text, n = pat.subn(repl, text, count=1)
        if n:
            break
    if text != original:
        backup = target.with_suffix(target.suffix + ".bak")
        backup.write_text(original, encoding="utf-8")
        target.write_text(text, encoding="utf-8")
        changed.append(str(target.relative_to(ROOT)))

if not changed:
    print("[BETA-PATCH] No obvious beta grid was found.")
    print("[BETA-PATCH] Examined:", ", ".join(examined) or "none")
    print("[BETA-PATCH] Manually search scripts/ for beta grid/candidates and replace it with:")
    print("[BETA-PATCH]", EXPANDED)
    print("[BETA-PATCH] Then rerun the cycle workflow and confirm new data/cycles.json includes beta values below 0.35 when warranted.")
    sys.exit(2)

print("[BETA-PATCH] Updated:", ", ".join(changed))
print("[BETA-PATCH] Backup files written with .bak suffix.")
print("[BETA-PATCH] Next: run the s2_event_horizon_cycle workflow and inspect beta distribution in the combined app.")
