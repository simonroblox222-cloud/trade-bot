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

## Rýchlosť a free limity

Všetko funguje zadarmo a bez kľúča: Jupiter lite API, DexScreener, pump.fun, RugCheck a verejné Solana RPC.
Bot nemá pevný interval. Beží tak rýchlo, ako dovolia limity, ktoré sú v `[rate_limits]` nastavené tesne pod free plány:

| API | predvolene req/min | poznámka |
|---|---|---|
| Jupiter quote | 55 | úzke hrdlo; 60 % ide na preceňovanie otvorených pozícií, zvyšok na nové vstupy |
| DexScreener profiles / pairs | 55 / 250 | |
| pump.fun | 20 | neoficiálne API |
| RugCheck | 20 | bez zverejneného limitu; summary pre RugCheck agenta, full report pre Critica |
| Solana RPC | 90 | verejné RPC je prísne; s voliteľným free Helius kľúčom ~500. Critic pridáva ~10 volaní na kandidáta (čerstvé peňaženky) |

- Každé API má vlastný token-bucket limiter, pri HTTP 429 sa API pozastaví podľa `Retry-After` (inak 30 s).
- Otvorené pozície majú prednosť: preceňujú sa v rámci `jupiter_mark_share` (pri 5 pozíciách cca každých 9 s)
  a nový rug check sa spustí len keď ostáva Jupiter budget (`min_jupiter_budget_for_entry`), inak sa odloží na ďalší cyklus.
- Keď sa minie budget na DexScreener profiles alebo pump.fun, Scanner použije posledný zoznam mintov.
- Dashboard ukazuje využitie každého API (req/min, 429, chyby) a počet fillov za hodinu.
- Kľúče (voliteľné) len cez env: `PAPERBOT_RPC_URL`, `JUPITER_API_KEY`, alebo `${PREMENNA}` v `config.toml`.

Hrubý odhad pri free limitoch: ~20 Jupiter quotes/min na nové vstupy = asi 6 až 7 nových tokenov
prejdených rug checkom za minútu; skutočný počet obchodov určujú signál, rug check a max. 5 otvorených pozícií.

## Agenti

```
Scanner → Social → RugCheck → Sizer → Critic → Executor → Logger → Equity
```

| Agent | Súbor | Úloha |
|---|---|---|
| Scanner | `agents.py`, `sources.py` | Nové páry z DexScreener (token profiles) a pump.fun (najnovšie coiny), štatistiky párov z DexScreener. Vstupný signál: likvidita, objem 1h, vek páru, momentum m5/h1, pomer nákupov/predajov. |
| Social | `critic.py` | Hype bez X API a bez scrapovania X: či token odkazuje na X účet, Telegram a web (DexScreener `info.socials`, pump.fun), počet pump.fun komentárov, „king of the hill“. Platený DexScreener boost sa ráta ako **mínus**. Číta sa len prítomnosť a počty, nikdy text (žiadny prompt injection). Tokeny pod `min_score` ďalej nejdú. |
| RugCheck | `rugcheck.py` | Mint a freeze authority, Token-2022 rozšírenia (permanent delegate, transfer hook, transfer fee…), koncentrácia top holderov (bez AMM vaultov a bonding curve), likvidita, LP burned/locked a skóre z RugCheck API, **simulácia predaja**: quote nákup → predaj späť, odmietne pri chýbajúcej sell route (honeypot) alebo veľkej strate (skrytá daň). |
| Sizer | `risk.py` | Veľkosť pozície (% equity, strop, minimum), max. počet pozícií, max. celková expozícia, rezerva na poplatky, denný limit straty. |
| Critic | `critic.py` | Posledná brána **pred** nákupom, hlavne ďalšia ochrana proti rugu tam, kam RugCheck agent nevidí: RugCheck full report (už rugnutý, insideri, insider siete = bundle/snipery, koľko drží dev, málo holderov), história dev peňaženky na pump.fun (sériový launcher, väčšina starších coinov mŕtva), čerstvé peňaženky medzi top holdermi (`getSignaturesForAddress`), odchod likvidity medzi skenmi, príliš rýchly hype (komentáre za minútu, objem vs. likvidita), boost bez X a bez komentárov. Plus disciplína: copycat symbol, max. pozícií na bonding curve a z jedného zdroja, pauza po sérii strát, naháňanie pumpy, veľkosť vs. likvidita, round trip blízko limitu. Len pravidlá, bez LLM. Iba schváli alebo zamietne, nič nemení. Chýbajúce dáta = poznámka, nie zamietnutie. Rug signál dá token na blacklist, disciplinárne dôvody len odložia. Každé rozhodnutie ide do tabuľky `critic`. |
| Executor | `execution.py` | Simulovaný fill z Jupiter quote: quote už obsahuje LP poplatky a price impact, navyše sa odráta `extra_slippage_bps`, base fee, priority fee, voliteľný Jito tip a rent za token účet (pri predaji sa vráti). Fill pod `otherAmountThreshold` = zlyhaný swap. Výstupy: stop-loss, take-profit, trailing stop, časový limit, odpis pri opakovane chýbajúcej sell route. |
| Logger | `store.py` | Všetko do SQLite: `trades` (každý fill s dôvodom, impactom, slippage a poplatkami), `positions`, `events`, `rugchecks`, `critic`, `equity`. |
| Equity | `agents.py` | Mark-to-market otvorených pozícií cez reálne sell quotes, equity krivka, UTC denný limit straty (pozastaví nové vstupy). |

Dashboard (`dashboard.py`, `dashboard.html`) iba číta SQLite: stav agentov, balance, equity, P&L, výhry/prehry,
equity krivka, živý log, otvorené pozície, história obchodov s dôvodmi, výsledky rug checku a rozhodnutia Critica
(schválené / zamietnuté s dôvodom a social skóre).

## Čo sme prevzali z research repozitárov

Kód z nich nie je skopírovaný ani spustený; prevzaté sú postupy (repozitáre boli len čítané):

- **Top holderi po peňaženkách** (`chainstacklabs/pump-fun-bot`): účty vlastnené programom (bonding curve,
  pool authority, vaulty) sú PDA mimo ed25519 krivky a nepočítajú sa; viac účtov jednej peňaženky sa sčíta.
  Test „on-curve“ je v `solana_keys.py` (čistý Python).
- **Oneskorenie** (`m3n4ce-dev/meme_traderv1` zmeral, že 0,5–2,5 s oneskorenie zmazalo celý paper zisk):
  po rozhodnutí sa čaká `latency_seconds`, spraví sa druhý quote a fill je za horšiu cenu. Ak cena medzitým
  ujde za slippage, swap „zlyhá“ a stratí sa len poplatok (ako naozaj). V `--mock` režime sa nečaká.
- **Edge check** (`meme_traderv1`): dashboard ukazuje priemerný obchod s 90 % bootstrap rozsahom,
  P&L bez 3 najlepších obchodov a verdikt, či je výsledok skutočný alebo šťastie (min. 30 obchodov).
- **Potvrdenie signálu** (`warp-id/solana-trading-bot`, `CONSECUTIVE_FILTER_MATCHES`): signál musí platiť
  `confirm_cycles` skenov po sebe, aby bot nenaletel na jednorazový výkyv.

Výpadok API (timeout, 429, 5xx) sa odlišuje od odpovede „neexistuje route“: pri výpadku sa pozícia neodpíše
a token sa nedá na blacklist, len sa to skúsi znova.

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
- Verejné Solana RPC má prísne rate limity; free Helius/QuickNode endpoint je voliteľný a pomôže s rýchlosťou rug checku.
- `getTokenLargestAccounts` vracia len 20 najväčších účtov, takže peňaženka rozdelená na veľa malých účtov sa nemusí celá započítať.
- RugCheck API je bez kľúča a môže mať limity; s `require_rugcheck_api = false` sa výpadok iba zaloguje.
- Formát RugCheck full reportu (`insiderNetworks`, `topHolders[].insider`, `creatorBalance`, `totalHolders`) a pump.fun
  zoznamu coinov autora nebol overený proti živému API (sandbox ho blokuje); čítajú sa opatrne a keď pole chýba,
  Critic zapíše poznámku namiesto zamietnutia. Pri prvom reálnom behu treba pozrieť panel Critica, či poznámky nehlásia chýbajúce dáta.
- Odmietnuté tokeny idú na blacklist natrvalo (aj pri nízkej likvidite), `reset` ho zmaže.

## Podklady

Návrh vychádza z `research/solana/` a `AUDIT.md`: filtre podobné `warp-id/solana-trading-bot` (burned LP, renounced mint,
veľkosť poolu), pump.fun tok z `chainstacklabs/pump-fun-bot`, paper prístup ako `sol-simulator` / `solana-paper-desk`
(volajú len DexScreener, Jupiter a verejné RPC). Žiadny cudzí kód nie je skopírovaný ani spustený a rozhodovanie
nepoužíva LLM (riziko prompt injection z názvov tokenov, na ktoré upozorňuje audit).
