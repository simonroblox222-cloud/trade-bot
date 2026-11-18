# Bezpečnostný audit repozitárov (18. 11. 2026)

Statická kontrola všetkých 27 repozitárov z `repos.txt` (shallow kópie z 18. 11. 2026). Kód nebol spustený.

## Čo sa kontrolovalo
- odosielanie private key / seed phrase na sieť (kľúčové slová pri `fetch`, `axios`, `requests`, webhookoch)
- všetky domény a IP adresy v kóde, neznáme ručne preverené
- inštalačné skripty (`preinstall`/`postinstall`/`prepare`, `setup.py`, `build.rs`)
- obfuskovaný kód, dlhé base64 bloby, extrémne dlhé riadky
- dynamické spúšťanie kódu (`eval`, `child_process`, `subprocess`, `os.system`)
- binárky a spustiteľné súbory
- závislosti npm a pip (typosquaty)

## Výsledok
**Malvér sa nenašiel.** Žiadny repozitár neposiela private key na cudzí server, nemá obfuskovaný kód, skryté binárky ani škodlivé inštalačné skripty. Všetky závislosti sú známe balíky.

Inštalačné skripty sú bežné vývojárske nástroje: `husky`, `simple-git-hooks`, `prisma generate` a kompilácia. Binárky sú testovacie fixtures (Meteora) a Gradle wrapper (solana-agent-kit).

## Na čo si dať pozor
| Repozitár | Zistenie |
|---|---|
| warp-id/solana-trading-bot | Voliteľný „warp executor“ posiela podpísané transakcie na `tx.warp.id` aj s poplatkovou transakciou pre warp. Kľúč neposiela. V predvolenom režime sa nepoužíva. |
| farfary/ScreenerBot | Klient komerčnej služby `screenerbot.io`: prihlásenie adresou peňaženky + podpisom, referral systém. Obsahuje PowerShell skript na opravu MSI inštalácie (vyžaduje admina). |
| hypnogaba/solana-signal-trader | Backend platformy `bornto.trade`, závislý od platených API (Solana Tracker, 6551.io, GoPlus). Vhodné na štúdium, nie na spustenie. |
| JeetVaidya1/SolTrader | Rozhodnutia deleguje na `claude --print` a vkladá do promptu názvy tokenov z trhu, čo otvára riziko prompt injection. S reálnymi peniazmi nepoužívať. |
| YZYLAB/solana-trade-bot | Závisí od platenej Solana Tracker API (`solana-swap`). |
| chainstacklabs, m3n4ce-dev | „Čudné“ domény (supabase, pinata, `evil.example`, `*.invalid`) sú len v testovacích dátach a fixtures. |

## Odporúčaný základ
- **Oficiálne SDK:** Jupiter, Raydium, Meteora, Jito, Helius, pump.fun docs.
- **Referenčné boty:** `chainstacklabs/pump-fun-bot`, `warp-id/solana-trading-bot` (predvolený executor).
- **Paper trading:** `switch-afk/sol-simulator`, `Hollylummen/solana-paper-desk`. Volajú len DexScreener, Jupiter a verejné RPC.

## Obmedzenia
Toto je vzorová statická kontrola s ručným preverením nálezov, nie úplný audit riadok po riadku. Veľké projekty (ScreenerBot ~2 000 súborov, solana-agent-kit ~1 000) neboli čítané celé. Kontrolované boli len závislosti uvedené v repozitároch, nie celé stromy tranzitívnych závislostí. Repozitáre sa môžu po tomto dátume zmeniť.

**Pravidlo:** žiadnemu botovi nikdy nedávaj private key hlavnej peňaženky, len samostatnú s malou sumou. Najprv vždy paper režim.
