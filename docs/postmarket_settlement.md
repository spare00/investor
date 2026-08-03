# Postmarket Settlement

Syncs account/positions/orders via reconciliation + position manager, computes FIFO P&L (`POSITION_LOT_METHOD=FIFO`), records overnight holdings. Broker is source of truth for balances; decision history is preserved.
