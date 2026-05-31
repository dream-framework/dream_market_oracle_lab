# DREAM Market Oracle · First-Notice Lab

Strict GitHub Pages app for coupling retained news cycles with scored market horizons, now defaulting to a First-Notice Filament Radar: small retained narrative structures growing faster than their public footprint.

## Source policy

Only public Pages artifacts are used.

Cycle artifacts:

- `https://dream-framework.github.io/s2_event_horizon_cycle/data/cycles.json`
- `https://dream-framework.github.io/s2_event_horizon_cycle/data/history.json`
- `https://dream-framework.github.io/s2_event_horizon_cycle/data/news_s2.json`

Market artifacts:

- `https://dream-framework.github.io/s2_signal_lab/data/live_predictions.csv`
- `https://dream-framework.github.io/s2_signal_lab/data/prediction_scorecard.csv`
- `https://dream-framework.github.io/s2_signal_lab/data/model_comparison.json`

No dummy rows. No page scraping. No zero-filled coupling rows. Live prediction rows are display-only and never used for hit/PnL.

## What changed in this build

- Default view is now **First notice**, focused on small fast retained filaments rather than already-worked public hubs.
- Added **First-Notice Filaments** table and chart with early score, obvious-hub penalty, growth multiple, acceleration, dust change, beta/lambda, Delta AIC change, source diversity change, event-shadow class, and why-not-obvious-hub explanation.
- S2 Theater now draws first-notice candidates as smaller sharper pulsing filaments while keeping large hubs as context.
- Popup now triggers on first-notice public-shadow outliers, not simply the largest hub.
- Larger, sharper typography across cards, charts, narratives, and tables.
- More explicit plain-English narration for source status, h1 dust, h5/h10/h20 coupling, beta audit, and table usage.
- Chart maximize/collapse retained; x-axis labels and narrative cards remain visible in max mode.
- Searchable/lazy tables retained for large live prediction state.
- Beta audit now separates the **legacy 0.35 floor** from the **expanded beta grid required upstream**.
- Added a browser-side **β playground**. This lets you change an assumed beta clock and floor-lock penalty to see how rankings would move. It is a what-if weighting layer, not a source refit.
- Added a **Postfactum PnL audit** from matured prediction score rows: hit lift, PnL lift, average win/loss proxy, win/loss ratio, cumulative paper proxy, and verdict.
- Includes `source_cycle_beta_patch/` for the upstream `s2_event_horizon_cycle` repo. The combined app cannot refit beta by itself; it reads published cycle JSON. Apply the upstream patch to test beta below 0.35 at the source.


## First-Notice Filament redesign

This build changes the default lens from obvious public hubs to early retained rumor filaments. The app now separates:

- **Established hubs**: large, obvious, already-public clusters used mostly as context.
- **Emerging filaments**: small but persistent structures with rising pressure.
- **Outlier filaments**: low-footprint clusters with extreme acceleration.
- **Possible pre-event shadows**: outlier filaments plus event-class language and market/sector coupling.

The build step emits a First-Notice radar row per retained topic/phrase cluster with:

- current size / public footprint
- growth multiple from a low base
- acceleration
- dust change
- beta and lambda
- Delta AIC change
- source diversity change
- event-shadow class
- obvious-hub penalty
- plain-English reason this is not just a worked-out hub

The early score is intentionally asymmetric: it rewards acceleration, growth, dust clearing, novelty, source diversity growth, semantic convergence, low saturation, and retained-tail strength, then subtracts an already-obvious penalty. The UI table, First Notice chart, Theater rings, and popup all use this score.

The S2 Theater still shows large hubs, but first-notice candidates are drawn as smaller sharper retained filaments with pulsing rings, so obvious hubs no longer dominate the visual field.

## Beta-grid note

The β playground is for academic sensitivity analysis. It does not alter historical artifacts or claim a new fitted beta. The combined app reads beta from published cycle artifacts. To actually allow beta below 0.35, patch and rerun the source cycle app. This package includes:

```text
source_cycle_beta_patch/config/beta_grid.yml
source_cycle_beta_patch/scripts/patch_beta_grid.py
```

Apply that patch to `s2_event_horizon_cycle`, rerun its workflow, then rerun this combined app workflow. If beta remains around 0.35 after lower values are available, the shallow tail is more credible. If beta shifts lower, the old 0.35 result was a floor artifact.

## Deploy combined app

1. Upload this repo to GitHub.
2. Settings → Pages → Source: GitHub Actions.
3. Actions → **Update and deploy S2 signal cycle lab** → Run workflow.
4. Open the Pages URL.

Generated bundle:

- `data/derived/signal_cycle_bundle.json`
- `data/derived/source_health.json`

These are deployed through GitHub Pages artifacts. They are not required to be committed back to the repo.

## Current-wave beta handling

This build uses `news_s2.json` / `cycle_active` as the primary cycle source when it is available. The archive remains available as context, but it no longer dominates the topic summaries or coupling rows. This matters after expanding the upstream beta grid below 0.35: current-wave beta values are now shown directly, while `β=0.35` is treated as a legacy-cluster diagnostic, not as the true floor.

## Paper Trading Board

This build adds a paper-only trading board. It keeps a local `$1,000` ledger in `data/state/paper_ledger.json`, exposes the public board in `data/derived/paper_trading.json`, and injects the same data into `data/derived/signal_cycle_bundle.json` for the dashboard.

Safety defaults:

- h1 never creates orders.
- live prediction rows are display-only until paired with non-h1 candidate coupling.
- local paper ledger is enabled by default.
- Alpaca paper submission is disabled unless explicitly enabled.
- no live-money endpoint is used by this app.

### Optional Alpaca paper trading

Create an Alpaca paper account, generate paper API keys, then add these GitHub settings:

Repository Secrets:

- `ALPACA_PAPER_KEY_ID`
- `ALPACA_PAPER_SECRET_KEY`

Repository Variables:

- `ALPACA_PAPER_ENABLED` = `true`
- `ALPACA_PAPER_BASE_URL` = `https://paper-api.alpaca.markets` optional; this is the default.

Optional risk variables can be edited in `.github/workflows/update.yml`:

- `PAPER_STARTING_CASH` default `1000`
- `PAPER_MAX_POSITION_NOTIONAL` default `125`
- `PAPER_MAX_TOTAL_EXPOSURE` default `400`
- `PAPER_MIN_CONFIDENCE` default `0.53`
- `PAPER_MIN_EXPECTED_RETURN` default `0.001`
- `PAPER_MAX_NEW_ORDERS` default `3`

Without Alpaca keys, the app still runs as a local paper simulator and proposed/fill rows remain in the DREAM dashboard only.


## Paper horizon safety

The paper board is deliberately horizon-gated. `h1` is display-only and never creates or closes paper positions. Entries require an h5/h10/h20 candidate coupling row plus a same-horizon live BUY vector. Existing legacy h1 positions, if any were created by an older build, are purged/closed by the h1 guard on the next paper-board run.

`history.json` from the news-cycle app is treated as context-only unless it contains fitted beta/lambda cycle rows. Coupling uses fitted rows from `cycles.json` and the active `news_s2.json` wave.

## S2 Filament Theater

This package adds a new **S2 Theater** tab to the main chart panel. The theater is built from the same public cycle artifacts fetched by the workflow. The build step emits `theater_batches` inside `data/derived/signal_cycle_bundle.json`; the browser animates those real fitted cycle rows only.

Rules:

- no dummy points
- no generated tickers
- no orders or advice
- replay last available cycle refresh batches only
- empty state if fitted cycle rows are not recognized

The animation phases are: raw cycle field → dust blur → cycle gate → λ scale gate → filaments → retained field.

### Theater circle tooltip fix

Each S2 Theater circle now carries a deterministic row identity and a row-specific source slice. Earlier builds attached the first topic-level headline block to every row in the same broad topic, which made multiple circles appear to have identical news content. The new build rotates the real source-row bucket by fitted row identity (`topic`, `phase`, `newest_peak`, `source`, `lambda`, `beta`, `Delta AIC`, `N`) and the browser also applies a compatibility fallback for older bundles. No dummy headlines are generated.
