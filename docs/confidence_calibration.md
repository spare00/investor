# Confidence Calibration

## Methods

- Calibration gap (avg confidence − accuracy)
- Expected Calibration Error (ECE) with minimum sample size gate

## API

`GET /performance/calibration` — firm ECE/gap plus `by_horizon` slices and `sample_count` / `min_sample_size`.

## Requirements

`min_calibration_sample_size` (default 30) — below threshold returns `INSUFFICIENT_DATA`.

## Horizon slices

Samples are grouped by `payload.universe_horizon` when present (else `unknown`). Each book uses the same min-sample gate independently.

## Limitations

- Samples come from `AgentOutcomeEvaluation.confidence` + payload flags (`direction_correct` or signed PnL/`actual_return`); sparse in early paper runs
- No per-agent bucket persistence unless POST evaluate-agents run
- Brier score stored on `AgentCalibrationRecord` but not auto-populated on every GET
- Observational only — does not auto-tune prompts or risk