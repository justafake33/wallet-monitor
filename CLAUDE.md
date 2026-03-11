# CLAUDE.md — wallet-monitor

This file provides context and conventions for AI assistants (Claude and others) working in this repository.

---

## Project Overview

**wallet-monitor** is a Python-based Solana wallet monitoring system. It tracks token purchases and sales across 4 wallets (2 bot-operated, 2 human-operated), scores token quality, and sends alerts via Telegram. Data is persisted in PostgreSQL and exposed through a JSON API for a dashboard.

- **Language**: Python 3
- **Framework**: Flask (web server + webhook receiver)
- **Database**: PostgreSQL (via psycopg2)
- **Deployment**: Render.com (worker service on port 8080)
- **External APIs**: Helius (Solana RPC), DexScreener, CoinGecko, Telegram Bot API

---

## Repository Structure

```
wallet-monitor/
├── monitor.py          # Entire application (~1,629 lines, monolithic)
├── requirements.txt    # Python dependencies
├── render.yaml         # Render.com deployment config
└── CLAUDE.md           # This file
```

There are **no tests**, **no submodules**, and **no separate config files**. Everything lives in `monitor.py`.

---

## Dependencies

From `requirements.txt`:

| Package | Purpose |
|---|---|
| `requests` | HTTP calls to DexScreener, Helius, Telegram |
| `pandas` | CSV report generation |
| `flask` | Web server, webhook endpoint, dashboard API |
| `psycopg2-binary` | PostgreSQL driver |

Install with:
```bash
pip install -r requirements.txt
```

---

## Running the Application

```bash
python monitor.py
```

The Flask server starts on port **8080**. On startup it:
1. Creates/migrates PostgreSQL tables (`init_db()`)
2. Restores pending trade state from DB (`db_carregar_estado()`)
3. Sends a Telegram boot notification
4. Schedules background tasks (calibration check every 6h, daily CSV report)

---

## Environment Variables

The application reads one environment variable:

| Variable | Default (hardcoded fallback) | Purpose |
|---|---|---|
| `DATABASE_URL` | `postgresql://postgres:...@switchyard.proxy.rlwy.net:47120/railway` | PostgreSQL connection string |

**All other credentials are currently hardcoded** in `monitor.py` (lines 14–18):
- `HELIUS_API_KEY`
- `TELEGRAM_TOKEN`
- `TELEGRAM_CHAT`
- `DASHBOARD_KEY` (plaintext: `neide12`)

> **Security note**: These should be moved to environment variables before any public exposure of this code.

---

## API Endpoints

| Method | Path | Auth | Description |
|---|---|---|---|
| `POST` | `/webhook` | None | Receives Helius transaction webhooks |
| `GET` | `/` | None | Health check — returns status and record counts |
| `GET` | `/dados` | `?key=neide12` | Full dashboard data (JSON) |
| `OPTIONS` | `*` | None | CORS preflight — all origins allowed |

### `/dados` Response Shape

```json
{
  "status": "ok",
  "versao": "6.3+db",
  "timestamp": "...",
  "resumo": { ... },
  "stats_carteiras": { ... },
  "tokens_ativos": [ ... ],
  "alertas_multi": [ ... ],
  "historico": [ ... ]
}
```

---

## Database Schema

Three PostgreSQL tables:

### `registros` (main trade log)
80+ columns. Key column groups:

| Group | Columns |
|---|---|
| Identity | `id`, `data_compra`, `carteira`, `token_mint`, `nome`, `dex`, `tipo` |
| Entry (T0) | `p_t0`, `mc_t0`, `liq_t0`, `volume_t0`, `score` |
| 5-min (T1) | `p_t1`, `mc_t1`, `liq_t1`, `volume_t1`, `txns5m_t1`, `buys_t1`, `sells_t1`, `var_t1_%`, `veredito_t1` |
| 15-min (T2) | same pattern with `_t2` |
| 45-min (T3) | same pattern with `_t3` |
| Holders | `holders_count`, `top1_pct`, `top10_pct`, `dev_saiu`, `bc_progress` |
| Final | `mc_pico`, `var_pico_%`, `categoria_final` |

### `deployers` (token creator reputation cache)
`dev_wallet` (PK), `tokens_total`, `tokens_rug`, `rug_rate`, `classificacao`, `ultima_update`

Classifications: `novo`, `confiavel`, `misto`, `rugger`, `serial_rugger`

### `signatures` (deduplication)
`sig` (PK) — prevents reprocessing the same webhook transaction.

---

## Core Architecture

### Data Flow

```
Helius Webhook → POST /webhook → processar_tx()
                                      ↓
                        extrair_mudancas_token()
                                      ↓
                  get_dados_token()   [DexScreener / Helius fallback]
                  get_dev_wallet()  → get_deployer_history()
                  get_holder_data()
                                      ↓
                          calcular_score()
                                      ↓
                      checar_multi_carteira()
                                      ↓
                  db_insert() + schedule async timers (T1/T2/T3)
                                      ↓
                  [checkpoints fire at 5min, 15min, 45min]
                                      ↓
                  categoria_final assigned at T3 → cleanup
```

### Checkpoint System

| Checkpoint | Delay | Purpose |
|---|---|---|
| T0 | Immediate | Entry price/metrics snapshot |
| T1 | 5 min | Early trend verdict |
| T2 | 15 min | Mid-term price action |
| T3 | 45 min | Final category assignment |
| Peak | 2/10/25 min | Tracking `mc_pico` (peak market cap) |

Checkpoints run in background threads (Python `threading.Timer`). State is held in-memory in the `estado` dict and persisted to DB.

---

## Token Scoring System (v5)

Calibrated on 680 tokens. Baseline winrate: 20.7%. Max score: ~14 pts, classified as:

| Score | Signal |
|---|---|
| ≥7 | ALTA CONFIANÇA |
| 4–6 | MODERADO |
| <4 | BAIXA CONFIANÇA |

### Scoring Components

| Factor | Range | Notes |
|---|---|---|
| Buy/Sell Ratio | -2 to +3 | ≥70% buys = max pts |
| Token Age | -2 to +2 | Sweet spot: 25–60 min |
| Market Cap at Entry | -2 to +2 | Sweet spot: $30k–$60k |
| Volume/MC Ratio | -1 to +1 | 1.0–3.0x optimal |
| Momentum (net txns) | -1 to +1 | Net ≥20 = +1 |
| Holders | -1 to +1 | ≥200 = +1 |
| Bonding Curve Progress | -1 to +1 | <30% early entry |
| Transaction Count | 0 to +1 | ≥80 txns |
| DEX Platform | 0 to +1 | pump.fun = +1 |
| Deployer History | -3 to +2 | serial_rugger = blocked entirely |
| UTC Hour multiplier | 0.85–1.15x | 2–8am = +15%, 6–8pm = -15% |

---

## Trade Categorization (T3 Final)

| Category | Criteria |
|---|---|
| VENCEDOR | Peak ≥+200%, held >+100% |
| PUMP & DUMP | Peak ≥+200%, collapsed <0% |
| BOM TRADE | Peak ≥+50%, held >+20% |
| ARMADILHA | Peak ≥+50%, dropped <-20% |
| CRESCIMENTO ESTÁVEL | Final ≥+20% |
| LATERAL | Final ±20% |
| MORREU | Final <-20% |
| DADOS INCOMPLETOS | Missing checkpoints |

---

## Multi-Wallet Detection

`checar_multi_carteira()` flags tokens bought by 2+ wallets within 60 minutes:

| Timing | Classification |
|---|---|
| <120s | SINCRONIZADO (coordinated) |
| 120s–10min | MULTI-CARTEIRA (synchronized) |
| 10–60min | MULTI-CARTEIRA (clustered) |

Alerts with `⭐` prefix when human wallets (C/D) are involved.

---

## Language and Naming Conventions

The codebase is written in **Brazilian Portuguese**. When modifying code, maintain this convention:

| Pattern | Example |
|---|---|
| Variable names | `carteira`, `token_mint`, `veredito`, `categoria_final` |
| Time suffixes | `_t0`, `_t1`, `_t2`, `_t3` |
| Boolean flags | `dev_saiu` (dev left), `multi_detectado` |
| DB column names | `mc_pico`, `var_pico_%`, `bc_progress` |
| Log emojis | 🆕 new, ✅ success, ⚠️ warning, 🚨 alert, 💀 dead, ⏱️ timer |

---

## Known Bugs

These are active bugs in the current codebase that should be fixed:

1. **`get_db()` called but never defined** (lines ~216, ~676, ~790)
   - Affected functions: `db_update_holders()`, `get_deployer_history()`
   - Fix: replace `get_db()` with `get_conn()`

2. **`DB_URL` undefined** (line ~1395 in `verificar_calibracao()`)
   - Fix: replace `DB_URL` with `DATABASE_URL`

---

## Development Conventions

- **No test suite exists.** Manual testing via webhook replay or direct function calls.
- **One file.** All logic is in `monitor.py`. Do not create additional source files unless there is a compelling reason.
- **Defensive error handling.** All external API calls are wrapped in `try/except`. Log failures, don't raise.
- **Thread safety.** The `estado` in-memory dict is accessed from multiple threads. Use `threading.Lock` if adding concurrent writes.
- **Database connections.** Always use `get_conn()` as a context manager: `with get_conn() as conn:`. Never leave connections open.
- **SQL safety.** Use parameterized queries (`%s` placeholders) — never f-strings or string concatenation in SQL.
- **Idempotency.** Webhook processing deduplicates via the `signatures` table. New transaction processors must check/insert there first.

---

## Git Workflow

- Commit directly on the working branch; no PR process is established.
- Commit messages follow: `"Update monitor.py"` (incremental changes) or descriptive one-liners.
- No automated CI/CD. Render.com auto-deploys on push to the configured branch.

---

## Background Tasks

| Task | Interval | Function |
|---|---|---|
| Score calibration check | Every 6h | `verificar_calibracao()` |
| Daily CSV report | Every 24h | `enviar_csv_diario()` |

Both are scheduled with `threading.Timer` inside the `startup()` function called at boot.

---

## Deployment Notes

- Hosted on **Render.com** as a `worker` service.
- Database hosted on **Railway** (PostgreSQL).
- The app is **stateful** (in-memory `estado` dict): horizontal scaling is not supported without refactoring state to the DB.
- Port: `8080` (set by Render environment).
