# Confidence Calibration

## Methods

- Calibration gap (avg confidence − accuracy)
- Expected Calibration Error (ECE) with minimum sample size gate

## API

`GET /performance/calibration`

## Requirements

`min_calibration_sample_size` (default 30) — below threshold returns `INSUFFICIENT_DATA`.

## Limitations

- Samples come from `AgentOutcomeEvaluation.confidence` + payload flags; sparse in early paper runs
- No per-agent bucket persistence unless POST evaluate-agents run
- Brier score stored on `AgentCalibrationRecord` but not auto-populated on every GET
