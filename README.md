# trade-bot

Solana memecoin paper-trading bot a podklady k nemu.

- `paperbot/` – paper-trading bot (Scanner → RugCheck → Sizer → Executor → Logger → Equity) s terminálovým dashboardom. Live režim je v kóde vypnutý. Návod: [paperbot/README.md](paperbot/README.md).
  `python -m paperbot run --mock` spustí offline demo, dashboard na http://127.0.0.1:8787.

- `research/solana/` – prehľad open-source nástrojov (Jupiter, Raydium, Jito, Helius, pump.fun, rug check, paper trading) a skript `fetch.sh` na ich lokálne stiahnutie.

Predchádzajúci akciový bot (Alpaca paper) bol odstránený; je dostupný v git histórii (commit `f345c5a`).
