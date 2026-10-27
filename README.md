# Trade bot (paper trading)

Multi-agentový obchodný bot pre akcie a komodity (cez ETF: GLD zlato, SLV striebro, USO ropa) na **Alpaca paper účte**. Žiadne skutočné peniaze: bot odmietne bežať, ak `ALPACA_PAPER` nie je `true` alebo ak číslo účtu nezačína `PA`.

## Agenti

| Agent | Súbor | Čo robí |
|---|---|---|
| Dáta | `tradebot/data.py` | sťahuje sviečky z Alpaca (zadarmo IEX feed), cache do CSV |
| Signál | `tradebot/strategies.py` | stratégie `rsi2` (návrat k priemeru), `trend` (Donchian), `orb` (intradenný breakout, na noc bez pozícií) |
| Riziko | `tradebot/risk.py` | max 1 % kapitálu na obchod, max 20 % v jednej pozícii, max 5 pozícií, stop obchodovania po -3 % za deň, úplné zastavenie po -15 % od vrcholu |
| Exekúcia | `tradebot/execution.py` | market príkaz s ochranným stop-lossom na Alpaca |
| Orchestrátor | `tradebot/orchestrator.py` | jeden cyklus dáta → signál → riziko → exekúcia, denník v `logs/` |
| Backtester | `tradebot/backtest.py` | rovnaký risk manager ako naživo, poplatky a sklz, porovnanie s buy & hold |

## Spustenie

```bash
pip install -r requirements.txt
cp .env.example .env          # doplň Alpaca PAPER kľúče
python main.py account        # overenie spojenia
python main.py backtest --strategy rsi2 --start 2016-01-01 --split 2022-01-01
python main.py run-once --strategy rsi2 --dry-run   # rozhodne, ale nepošle príkazy
python main.py live --strategy orb --timeframe 5m   # intradenná slučka
python -m pytest -q
```

Denná stratégia sa dá spúšťať automaticky cez GitHub Actions (`.github/workflows/paper-trade.yml`, každý pracovný deň pred otvorením trhu). Treba v repo nastaviť secrets `ALPACA_API_KEY` a `ALPACA_SECRET_KEY`.

## Úprimne o ziskovosti

Väčšina day tradingových stratégií po poplatkoch a sklze neporazí obyčajné držanie indexu. Preto: každá stratégia sa najprv overí backtestom s rozdelením na in-sample a out-of-sample obdobie, potom aspoň 1 až 3 mesiace na paper účte. Do skutočných peňazí nič nejde bez výslovného súhlasu.
