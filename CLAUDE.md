# CLAUDE.md — Wallet Monitor

This file provides context and conventions for AI assistants working on this codebase.

---

## Project Overview

**wallet-monitor** is a Solana blockchain wallet monitoring system (v6.1). It watches a fixed set of Solana wallets for token purchases and sales, scores token quality, detects coordinated multi-wallet buys, and sends Telegram alerts with detailed analytics.

The entire application is a single Python file (`monitor.py`, ~827 lines) deployed as a background worker on Render.com.

---

## Technology Stack

| Layer | Technology |
|---|---|
| Language | Python 3.x |
| Web framework | Flask |
| Data processing | Pandas |
| HTTP client | Requests |
| Deployment | Render.com (worker service) |
| Blockchain node | Helius RPC (Solana) |
| Notifications | Telegram Bot API |

---

## File Layout

```
wallet-monitor/
├── monitor.py          # Entire application (~827 lines)
├── requirements.txt    # Python dependencies: requests, pandas, flask
├── render.yaml         # Render.com deployment config
└── CLAUDE.md           # This file
```

Runtime output files (not in git):
- `monitoramento_carteira_A.csv`
- `monitoramento_carteira_B.csv`
- `monitoramento_carteira_C.csv`

---

## Architecture and Data Flow

```
Helius Webhook → POST /webhook
        │
        ▼
  Deduplication (signatures_vistas set)
        │
        ▼
  Transaction Parser  (agnostic: Axiom / Photon / Trojan / Jupiter / pump.fun)
        │
        ▼
  Token Data Fetch    DexScreener → pump.fun bonding curve fallback
        │
        ▼
  Quality Score       0–10 (volume/MC, txn count, liquidity, age, DEX)
        │
        ▼
  Multi-wallet check  mints_globais → Telegram alert if coordinated buy
        │
        ▼
  Checkpoint scheduler  T1=5 min | T2=15 min | T3=45 min
        │
        ▼
  CSV export          every 10 minutes (SALVAR_A_CADA)
```

---

## Key Sections in monitor.py

| Lines | Purpose |
|---|---|
| 1–25 | Imports, Flask app init |
| 26–46 | Hardcoded config (API keys, wallets, webhook URL) |
| 48–62 | Global state (`estado`, `mints_globais`, `signatures_vistas`) |
| 91–128 | `calcular_score_qualidade()` — quality scoring 0–10 |
| 169–238 | `get_token_data()` — DexScreener + pump.fun fallback |
| 239–256 | `get_sol_price()` — CoinGecko SOL price |
| 257–344 | `get_holders_data()` — holder counts, top-holder %, dev-exit detection |
| 350–400 | `parse_transaction_agnostico()` — universal transaction parser |
| 406–469 | `verificar_multi_carteira()` — coordinated-buy detection + Telegram alert |
| 475–512 | `processar_venda()` — sale processing and P&L calculation |
| 518–607 | `verificar_checkpoints()` — T1/T2/T3 checkpoint logic |
| 608–740 | `processar_transacao()` — main per-transaction handler |
| 741–746 | `salvar_csv()` — CSV export |
| 752–776 | `POST /webhook` route |
| 779–791 | `GET /` health-check route |
| 797–827 | Startup sequence and Flask `app.run()` |

---

## External API Integrations

| API | Purpose | Base URL |
|---|---|---|
| Helius RPC | Transaction parsing, token accounts | `https://mainnet.helius-rpc.com/?api-key=...` |
| DexScreener | Token price, MC, liquidity, volume | `https://api.dexscreener.com/latest/dex/tokens/{mint}` |
| pump.fun | Bonding curve data, holders | `https://frontend-api.pump.fun/coins/{mint}` |
| CoinGecko | SOL/USD price | `https://api.coingecko.com/api/v3/simple/price` |
| Telegram Bot API | Alerts | `https://api.telegram.org/bot{token}/sendMessage` |

---

## Configuration (Currently Hardcoded)

All configuration lives at the top of `monitor.py` (lines 26–46). There is no `.env` file or environment-variable loading. Values to be aware of:

- `HELIUS_API_KEY` — Helius RPC access key
- `TELEGRAM_TOKEN` / `TELEGRAM_CHAT` — bot credentials and group chat ID
- `CARTEIRAS` — dict mapping wallet address → friendly name (`carteira_A/B/C`)
- `WEBHOOK_URL` — public URL registered with Helius for delivery
- `SALVAR_A_CADA` — CSV save interval in minutes (default: 10)
- `TOKENS_IGNORADOS` — set of mints to skip (SOL, USDC, USDT, blacklisted)

**Convention:** When adding new wallets or tokens to ignore, edit the dicts/sets at the top of the file; do not scatter configuration deeper in the code.

---

## Quality Scoring System

`calcular_score_qualidade()` returns a `(score, emoji, description)` tuple:

| Criteria | Max points | Notes |
|---|---|---|
| Volume / MC ratio | +3 | Most important signal |
| Transaction count | +2 | 100–450 txns is optimal |
| Liquidity = $0 | +2 | Means still on bonding curve |
| Token age ≤ 15 min | +2 | Freshness bonus |
| DEX is pump.fun | +1 | Platform bonus |
| Low vol/MC ratio | −2 | No traction penalty |

Score thresholds:
- `🟢 ≥ 7` — High Confidence
- `🟡 4–6` — Moderate
- `🔴 < 4` — Low Confidence

---

## Checkpoint System

Each tracked token goes through three timed checkpoints after the initial purchase:

| Checkpoint | Delay | Key actions |
|---|---|---|
| T1 | 5 min | Exit alert if gain ≥ 50 % (⚠️) or ≥ 100 % (🚨) |
| T2 | 15 min | Update metrics |
| T3 | 45 min | Final categorization, remove from pending, save CSV |

**Outcome categories at T3:**
- `🏆 WINNER` — strong rise, sustained
- `🎯 PUMP & DUMP` — rise then collapse
- `📈 GOOD TRADE` — solid growth
- `⚠️ TRAP` — quick spike then fall
- `📊 STABLE GROWTH`
- `➡️ LATERAL` — little movement
- `💀 DIED` — consistent decline

---

## Multi-Wallet Detection

`mints_globais` tracks the first purchase timestamp of each mint per wallet. `verificar_multi_carteira()` fires a Telegram alert when two or more monitored wallets buy the same token within 60 minutes:

| Time gap | Severity |
|---|---|
| < 2 min | 🚨🚨 SYNCHRONIZED |
| 2–10 min | 🚨 MULTI-WALLET ALERT |
| 10–60 min | ℹ️ MULTI-WALLET INFO |

---

## Transaction Parser

`parse_transaction_agnostico()` supports multiple DEX/bot sources:

- **Axiom** — MEV bot data
- **Photon** — trading dashboard
- **Trojan** — trading bot
- **Jupiter** — swap aggregator
- **pump.fun** — token launcher

Two methods tried in order:
1. `tokenBalanceChanges` (Helius enhanced mode) — preferred
2. `tokenTransfers` (fallback for aggregators)

Returns `[{mint, amount}]` where positive = BUY, negative = SELL.

---

## Webhook API

### `POST /webhook`
Receives Helius transaction webhooks. Accepts a JSON array of transactions.

Minimal request shape:
```json
[
  {
    "signature": "string",
    "timestamp": 1234567890,
    "type": "string",
    "source": "SYSTEM_PROGRAM",
    "accountData": [
      {
        "account": "<wallet_address>",
        "tokenBalanceChanges": [ ... ],
        "tokenTransfers": [ ... ]
      }
    ]
  }
]
```

### `GET /`
Health check. Returns:
```json
{
  "status": "running v6.1",
  "registros": 42,
  "compras": 30,
  "vendas": 12,
  "pendentes": 5
}
```

---

## Development Workflow

### Running Locally

```bash
pip install -r requirements.txt
python monitor.py
```

The app starts on `0.0.0.0:8080` (or the value of the `PORT` environment variable).

### Deployment

Render.com reads `render.yaml`:
```yaml
services:
  - type: worker
    name: wallet-monitor
    runtime: python
    buildCommand: pip install -r requirements.txt
    startCommand: python monitor.py
```

Push to the tracked branch; Render auto-deploys.

### Dependency Updates

Add packages to `requirements.txt` (plain name, no version pins currently). Keep the list minimal.

---

## Code Conventions

1. **Language:** Comments, variable names, and Telegram messages are written in **Portuguese**. Maintain that convention when editing existing code. New functionality can use English if the author prefers, but be consistent within a section.

2. **Single-file approach:** All logic lives in `monitor.py`. Do not split into modules unless the file grows substantially beyond its current size or the author requests it.

3. **State management:** Global mutable state (`estado`, `mints_globais`, `signatures_vistas`) is accessed without locks. The Flask dev server is single-threaded by default — keep it that way to avoid race conditions, or add threading primitives if concurrency is enabled.

4. **Error handling:** Most API calls are wrapped in `try/except` blocks that return `None` on failure. Continue this pattern; do not let individual API failures crash the process.

5. **Telegram formatting:** Use HTML (`parse_mode="HTML"`), `<b>` for bold, and emoji for visual hierarchy. Do not switch to Markdown mode.

6. **No tests:** There is no test suite. When making changes, test manually by sending sample webhook payloads to the running server.

7. **Hardcoded secrets:** Credentials are currently embedded in the source. Do not commit new secrets; prefer extracting them to environment variables (`os.getenv`) when touching that code.

---

## Known Limitations / Areas for Improvement

- Credentials are hardcoded (should use `os.getenv` + a `.env` file or Render environment variables).
- No test suite.
- Flask is run in development mode (`debug` not set explicitly); consider `waitress` or `gunicorn` for production.
- State is in-memory only; a process restart loses all pending checkpoints and known tokens.
- CSV files accumulate unboundedly on disk.
- No structured logging (uses `print`).

---

## Git Conventions

- Repository: `justafake33/wallet-monitor`
- Default branch: `master`
- Development branch for AI-assisted work: `claude/claude-md-mm9ig6i9tfmb717x-ISQyD`
- Commit messages are plain English imperatives (e.g., `Update monitor.py`).
- Push with: `git push -u origin <branch-name>`
