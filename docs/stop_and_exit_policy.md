# Stop and Exit Policy

Supports FIXED_PRICE, PERCENTAGE, ATR_BASED, TIME_BASED stops; multi-target take-profit with partial exits. Stop widening blocked by default; tightening allowed. Hard stops create Order Intents — `AUTO_EXECUTE_HARD_STOPS=false` keeps them pending approval. Invalidation states: NOT_TRIGGERED / POSSIBLE / CONFIRMED / UNKNOWN_DUE_TO_DATA.
