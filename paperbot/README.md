# paperbot – Solana memecoin paper-trading bot

Simulované obchodovanie memecoinov na Solane. **Len paper režim:** žiadne transakcie, žiadne private keys,
žiadne burzové API kľúče. Ceny a filly pochádzajú z verejného Jupiter `/quote` API, nikdy sa nevolá `/swap`.

Čistý Python 3.11+ bez závislostí tretích strán (stdlib: `urllib`, `sqlite3`, `http.server`, `tomllib`).

## Spustenie

```bash
# offline demo so syntetickým trhom (bez siete)
python -m paperbot run --mock --tick-seconds 1

# reálne dáta (DexScreener + pump.fun + Jupiter quotes + RugCheck + Solana RPC)
cp config.example.toml config.toml
python -m paperbot run

# dashboard je na http://127.0.0.1:8787 (spúšťa sa spolu s botom),
# samostatne nad existujúcou DB:
python -m paperbot dashboard

python -m paperbot reset          # zmaže paper účet (SQLite)
python -m unittest discover tests  # testy
```

## Agenti

```
Scanner → RugCheck → Sizer → Executor → Logger → Equity
```

| Agent | Súbor | Úloha |
|---|---|---|
| Scanner | `agents.py`, `sources.py` | Nové páry z DexScreener (token profiles) a pump.fun (najnovšie coiny), štatistiky párov z DexScreener. Vstupný signál: likvidita, objem 1h, vek páru, momentum m5/h1, pomer nákupov/predajov. |
| RugCheck | `rugcheck.py` | Mint a freeze authority, Token-2022 rozšírenia (permanent delegate, transfer hook, transfer fee…), koncentrácia top holderov (bez AMM vaultov a bonding curve), likvidita, LP burned/locked a skóre z RugCheck API, **simulácia predaja**: quote nákup → predaj späť, odmietne pri chýbajúcej sell route (honeypot) alebo veľkej strate (skrytá daň). |
| Sizer | `risk.py` | Veľkosť pozície (% equity, strop, minimum), max. počet pozícií, max. celková expozícia, rezerva na poplatky, denný limit straty. |
| Executor | `execution.py` | Simulovaný fill z Jupiter quote: quote už obsahuje LP poplatky a price impact, navyše sa odráta `extra_slippage_bps`, base fee, priority fee, voliteľný Jito tip a rent za token účet (pri predaji sa vráti). Fill pod `otherAmountThreshold` = zlyhaný swap. Výstupy: stop-loss, take-profit, trailing stop, časový limit, odpis pri opakovane chýbajúcej sell route. |
| Logger | `store.py` | Všetko do SQLite: `trades` (každý fill s dôvodom, impactom, slippage a poplatkami), `positions`, `events`, `rugchecks`, `equity`. |
| Equity | `agents.py` | Mark-to-market otvorených pozícií cez reálne sell quotes, equity krivka, UTC denný limit straty (pozastaví nové vstupy). |

Dashboard (`dashboard.py`, `dashboard.html`) iba číta SQLite: stav agentov, balance, equity, P&L, výhry/prehry,
equity krivka, živý log, otvorené pozície, história obchodov s dôvodmi, výsledky rug checku.

## Live režim je natvrdo vypnutý

- `config.LIVE_TRADING_ENABLED = False` je konštanta v kóde, nie hodnota z configu ani env.
- `load_config` odmietne akýkoľvek `mode` okrem `"paper"`.
- `make_execution()` pre iný režim vyhodí `LiveTradingDisabled`; `LiveExecution` sa nedá ani vytvoriť.
- V balíku nie je kód na prácu s kľúčmi, podpisovanie ani odosielanie transakcií (test to kontroluje).

Architektúra je pripravená na neskorší live backend: stačí implementovať `LiveExecution` s rovnakým rozhraním
ako `PaperExecution` (`simulate_buy` / `simulate_sell` → reálne swapy) a vedome zmeniť konštantu. Predtým:
samostatná peňaženka s malou sumou (nikdy hlavná), Jito/MEV ochrana, potvrdzovanie transakcií, a výsledky z paper režimu
porovnať s reálnymi fillmi.

## Obmedzenia, na ktoré treba myslieť

- **Paper výsledky sú optimistické.** Reálne swapy majú latenciu, sandwich útoky a zlyhané transakcie; preto
  pesimistický `extra_slippage_bps`. Výsledky z `--mock` sú syntetické a nehovoria nič o ziskovosti stratégie.
- Ukážková momentum stratégia je len východisko, nie overená výhoda.
- pump.fun frontend API je neoficiálne (môže blokovať alebo sa zmeniť); vtedy Scanner zapíše varovanie a ide ďalej s DexScreener.
- Verejné Solana RPC má prísne rate limity; na reálny beh odporúčam vlastný RPC endpoint.
- Holder koncentrácia vynecháva iba známe AMM autority (Raydium AMM v4, CPMM) a pri bonding curve najväčší účet. Iné DEX vaulty (Meteora, PumpSwap) môžu koncentráciu nadhodnotiť, dopíš ich do `pool_owner_allowlist`.
- RugCheck API je bez kľúča a môže mať limity; s `require_rugcheck_api = false` sa výpadok iba zaloguje.
- Odmietnuté tokeny idú na blacklist natrvalo (aj pri nízkej likvidite), `reset` ho zmaže.

## Podklady

Návrh vychádza z `research/solana/` a `AUDIT.md`: filtre podobné `warp-id/solana-trading-bot` (burned LP, renounced mint,
veľkosť poolu), pump.fun tok z `chainstacklabs/pump-fun-bot`, paper prístup ako `sol-simulator` / `solana-paper-desk`
(volajú len DexScreener, Jupiter a verejné RPC). Žiadny cudzí kód nie je skopírovaný ani spustený a rozhodovanie
nepoužíva LLM (riziko prompt injection z názvov tokenov, na ktoré upozorňuje audit).
