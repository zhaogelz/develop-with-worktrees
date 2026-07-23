# Changelog

## 0.2.0-beta.1 — 2026-07-23

- Clean breaking release: verification policy is schema 3 only.
- Starts and integrates against the recorded current local base branch.
- Adds machine-global weighted FIFO validation capacity, local capacity settings, duration estimates, and slow-validation advisory.
- Makes cache cleanup opt-in and plan-bound; links, junctions, `.env*`, and changed targets stop the whole prune.
- Adds CLI version contract, candidate-policy approval, and release consistency tests.
