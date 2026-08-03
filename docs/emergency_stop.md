# Emergency Stop

Applies at the execution layer (`TradingControls` + broker cancel).

On engage:

1. Block new intents / approvals / submits
2. Cancel open broker orders when `EMERGENCY_STOP_CANCEL_OPEN_ORDERS=true` (default)
3. **Do not** close positions unless `EMERGENCY_STOP_CLOSE_POSITIONS=true` (default false)
4. Persist control state (`ops_persistence`) so restart keeps the stop
5. Clear requires explicit `/operations/emergency-stop/clear` (or `/trading/clear-emergency`) then resume

Endpoints: `POST /operations/emergency-stop`, `POST /operations/emergency-stop/clear`, `POST /trading/emergency-stop`.
