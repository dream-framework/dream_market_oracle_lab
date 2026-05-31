# Upstream cycle beta-grid patch

The combined signal-cycle app **does not refit news cycles**. It reads the JSON published by `s2_event_horizon_cycle`.

To truly test whether beta=0.35 is real or just a floor artifact, apply this patch to the upstream `s2_event_horizon_cycle` repo and rerun its workflow.

## Why

Current cycle artifacts repeatedly select beta=0.35. That means the fit is choosing the lowest allowed curve-shape value. It may be a real attractor, or it may mean the true value wants to go lower.

## Patch

From the root of the `s2_event_horizon_cycle` repo, copy this folder into the repo and run:

```bash
python source_cycle_beta_patch/scripts/patch_beta_grid.py
```

The intended beta grid is:

```text
0.15, 0.20, 0.25, 0.30, 0.35, 0.40, 0.45, 0.55, 0.65, 0.75, 0.85, 1.00, 1.15, 1.35, 1.50, 1.75, 2.00, 2.25
```

Then run the cycle workflow. The combined app will read the new `cycles.json`, `history.json`, and `news_s2.json` and show whether the beta floor-lock persists.

## Interpretation

- If beta remains near 0.35 even after lower values are available, 0.35 is likely meaningful.
- If beta shifts to 0.15–0.30, the old 0.35 result was a floor artifact.
- In either case, cycle pressure can still be used as a clock; exact beta should remain under audit until this refit is done.
