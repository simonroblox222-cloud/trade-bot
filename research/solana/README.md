# Solana memecoin – výskum (zbierka z GitHubu, 18. 11. 2026)

Cudzí kód tu nie je uložený, iba zoznam (`repos.txt`) a skript `fetch.sh`, ktorý repozitáre stiahne lokálne do `vendor/` (ignorované gitom). **Nič z toho nebolo spustené.** Predtým, než čokoľvek spustíš:
- nikdy nedávaj private key hlavnej peňaženky, len prázdnu testovaciu,
- prečítaj si `package.json` / `requirements.txt` a hľadaj neznáme závislosti alebo URL, kam sa posielajú dáta,
- najprv iba paper/simulačný režim.

Bezpečnostný audit všetkých repozitárov: [AUDIT.md](AUDIT.md).

## 1. Oficiálne SDK a infra (najdôveryhodnejšie)
| Repozitár | Na čo |
|---|---|
| [jup-ag/jupiter-quote-api-node](https://github.com/jup-ag/jupiter-quote-api-node) | Jupiter quote/swap API pre Node – základ pre paper fills aj reálne swapy |
| [jup-ag/jupiter-swap-api-client](https://github.com/jup-ag/jupiter-swap-api-client) | To isté v Ruste |
| [raydium-io/raydium-sdk-V2](https://github.com/raydium-io/raydium-sdk-V2) | Raydium pooly, swapy |
| [MeteoraAg/dlmm-sdk](https://github.com/MeteoraAg/dlmm-sdk) | Meteora DLMM pooly |
| [jito-labs/jito-ts](https://github.com/jito-labs/jito-ts) | Jito bundles (ochrana pred sandwich/MEV) |
| [helius-labs/helius-sdk](https://github.com/helius-labs/helius-sdk) | Helius RPC, webhooky, DAS API (sledovanie peňaženiek, nové tokeny) |
| [pump-fun/pump-public-docs](https://github.com/pump-fun/pump-public-docs) | Oficiálne IDL a dokumentácia pump.fun programov |
| [wkennedy/helius-webhooks-tutorial](https://github.com/wkennedy/helius-webhooks-tutorial) | Príklady Helius webhookov (TS + Rust) |

## 2. Kompletné boty / referenčné implementácie
| Repozitár | Poznámka |
|---|---|
| [warp-id/solana-trading-bot](https://github.com/warp-id/solana-trading-bot) | Najznámejší open-source Raydium bot (TS), filtre: burned LP, mint renounced, pool size |
| [chainstacklabs/pump-fun-bot](https://github.com/chainstacklabs/pump-fun-bot) | Pump.fun trading/sniping bot v Pythone, bez 3rd-party API, s cookbookom |
| [farfary/ScreenerBot](https://github.com/farfary/ScreenerBot) | Rust: screener s rug filtrom, auto-trader, dashboard (veľký projekt) |
| [sendaifun/solana-agent-kit](https://github.com/sendaifun/solana-agent-kit) | AI-agent toolkit pre Solanu (Jupiter, Pump.fun, rugcheck akcie…) |
| [YZYLAB/solana-trade-bot](https://github.com/YZYLAB/solana-trade-bot) | Jednoduché príklady swapov: Raydium, Pumpfun, Orca, Jupiter |
| [henrytirla/Solana-Trading-Bot](https://github.com/henrytirla/Solana-Trading-Bot) | Python: Raydium + Pump.fun + Jito + priority fees |
| [JeetVaidya1/SolTrader](https://github.com/JeetVaidya1/SolTrader) | Malý JS bot: monitor pump.fun → analýza → executor |
| [Mason333xbt/sol-terminal](https://github.com/Mason333xbt/sol-terminal) | Memecoin terminál: PumpPortal + DexScreener feedy |
| [hypnogaba/solana-signal-trader](https://github.com/hypnogaba/solana-signal-trader) | Python: Telegram signály + 14-bodový rug-risk check |
| [jagelous/sol-jupiv6-bundle](https://github.com/jagelous/sol-jupiv6-bundle) | Jupiter v6 swap cez Jito bundle (API server) |
| [0xjeffro/PumpScan](https://github.com/0xjeffro/PumpScan) | Go: indexer pump.fun transakcií cez Helius webhook |

## 3. Rug check / skenery
| Repozitár | Poznámka |
|---|---|
| [degenfrends/solana-rugchecker](https://github.com/degenfrends/solana-rugchecker) | JS knižnica: mint/freeze authority, holderi, LP |
| [ccan23/rugcheck](https://github.com/ccan23/rugcheck) | Python wrapper pre RugCheck API |
| [Dairus01/solana-rugcheck-detector](https://github.com/Dairus01/solana-rugcheck-detector) | Real-time monitoring nových tokenov + kontroly |
| [0xdariel/solana-token-scanner](https://github.com/0xdariel/solana-token-scanner) | Python: rug detekcia, holderi, hĺbka likvidity |
| [Kabutoxyz/solana-token-scanner](https://github.com/Kabutoxyz/solana-token-scanner) | Python: market cap, kvalita holderov, momentum |

## 4. Paper trading / simulácia (najbližšie k tomu, čo chceš)
| Repozitár | Poznámka |
|---|---|
| [m3n4ce-dev/meme_traderv1](https://github.com/m3n4ce-dev/meme_traderv1) | „Paper-first“ pump.fun bot: rug screening, graduation plays |
| [switch-afk/sol-simulator](https://github.com/switch-afk/sol-simulator) | Paper trading s live cenami, limit orders, DCA, copy trade |
| [zevsmoura/solana-memecoin-paper-trader](https://github.com/zevsmoura/solana-memecoin-paper-trader) | Next.js „hra“ s virtuálnym SOL a live dátami |
| [Hollylummen/solana-paper-desk](https://github.com/Hollylummen/solana-paper-desk) | Python paper-trading dashboard |
| [irhagram/scalp-dashboard](https://github.com/irhagram/scalp-dashboard) | Jednosúborový HTML simulačný dashboard |

## Vynechané zámerne
- Desiatky „sniper / copy-trading“ repozitárov s kľúčovými slovami v popise, Telegram kontaktmi a „free premium“ sľubmi.
  Tento typ je bežný vektor krádeže private keys.
- `WolfUntilangel/sentry-bot-resp` – popis sľubuje bota, obsah je prázdny/nesúvisiaci (fake repo).

## Stiahnutie lokálne
```bash
cd research/solana
./fetch.sh            # shallow clone všetkých repozitárov do vendor/
```
