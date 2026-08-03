# Position Monitor

`PositionMonitor` watches `position_lifecycles` (separate from broker `positions` mirror). Verdicts: HEALTHY → EMERGENCY_ACTION_REQUIRED. Never calls Broker submit APIs — emits events and snapshots only.
