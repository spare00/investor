# Position Monitor

`PositionMonitor` watches `position_lifecycles` (separate from broker `positions` mirror). Verdicts: HEALTHY → EMERGENCY_ACTION_REQUIRED. Never calls Broker submit APIs — emits events and snapshots only.

## Unattended path

Scheduler `intraday_eval_*` → `DailyWorkflowService.evaluate_intraday` always runs `IntradayService.monitor_all` first (when `ENABLE_INTRADAY_MONITORING=true`). Stop / take-profit / max-holding events set `requires_analysis=true` and escalate the tick to `trigger=risk_change`, which bypasses the interval cooldown and feeds `trigger_event_ids` into `IntradayAgentService`. Hard stops may also create exit intents via the exit-policy check inside `monitor_all`.
