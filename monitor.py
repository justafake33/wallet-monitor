import requests
import pandas as pd
import time
import threading
import os
import json
import psycopg2
import psycopg2.extras
from datetime import datetime
from flask import Flask, request, jsonify

# v6.3 + PostgreSQL — dados persistentes entre restarts

HELIUS_API_KEY = "4f586430-90ef-4c8f-9800-b98bfe5f1151"
TELEGRAM_TOKEN = "8319320909:AAFnhGkFS1YxhthhE4RolutJScEjBCjIvrA"
TELEGRAM_CHAT  = "-5284184650"
DASHBOARD_KEY  = "neide12"
DATABASE_URL   = os.environ.get("DATABASE_URL", "postgresql://postgres:OgNvgWkjcpuFxZPHBaASjCKnLNsXKlpI@switchyard.proxy.rlwy.net:47120/railway")

CARTEIRAS = {
    "GijFWw4oNyh9ko3FaZforNsi3jk6wDovARpkKahPD4o5": "carteira_A",
    "ANfB2knFb7pC7jKadHnSP4xKZ31KJGNLhWRo89LWsFeW": "carteira_B",
    "43C9gHfJ7YgqKv5ft3hodFgumydv1nEiNHD1PuANufk5": "carteira_C",
    "EvGpkcSBfhp5K9SNP48wVtfNXdKYRBiK3kvMkB66kU3Q": "carteira_D",
}

TIPO_CARTEIRA = {
    "carteira_A": "bot",
    "carteira_B": "bot",
    "carteira_C": "humano",
    "carteira_D": "humano",
}

TOKENS_IGNORAR = {
    "So11111111111111111111111111111111111111112",
    "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
    "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB",
    "8S4Hk9bMLTTCBzBrFGSRcPbHiWbVXKpmWHvEMPEELXXt",
    "11111111111111111111111111111111",
}

# Estado em memória (cache — o banco é a fonte de verdade)
estado = {
    nome: {
        "tokens_conhecidos": set(),
        "registros":         [],
        "pendentes":         {},
    }
    for nome in set(CARTEIRAS.values())
}

mints_globais     = {}
signatures_vistas = set()
app = Flask(__name__)

@app.after_request
def add_cors(response):
    response.headers["Access-Control-Allow-Origin"]  = "*"
    response.headers["Access-Control-Allow-Headers"] = "Content-Type"
    response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    return response


# ══════════════════════════════════════════════════════════
# BANCO DE DADOS
# ══════════════════════════════════════════════════════════
def get_conn():
    return psycopg2.connect(DATABASE_URL)


def init_db():
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS registros (
                    id              SERIAL PRIMARY KEY,
                    data_compra     TIMESTAMP,
                    carteira        TEXT,
                    tipo_carteira   TEXT,
                    token_mint      TEXT,
                    nome            TEXT,
                    dex             TEXT,
                    fonte_dados     TEXT,
                    quantidade      FLOAT,
                    signature       TEXT,
                    tipo            TEXT,
                    is_multi        BOOLEAN DEFAULT FALSE,
                    p_t0            FLOAT,
                    mc_t0           FLOAT,
                    liq_t0          FLOAT,
                    volume_t0       FLOAT,
                    txns5m_t0       INT,
                    buys_t0         INT,
                    sells_t0        INT,
                    net_momentum_t0 INT,
                    idade_min       FLOAT,
                    token_antigo    TEXT,
                    ratio_vol_mc_t0 FLOAT,
                    score_qualidade INT,
                    holders_count   INT,
                    top1_pct        FLOAT,
                    top10_pct       FLOAT,
                    dev_saiu        BOOLEAN,
                    bc_progress     FLOAT,
                    p_t1 FLOAT, mc_t1 FLOAT, liq_t1 FLOAT, volume_t1 FLOAT,
                    txns5m_t1 INT, buys_t1 INT, sells_t1 INT,
                    ratio_vol_mc_t1 FLOAT, var_t1 FLOAT, veredito_t1 TEXT,
                    p_t2 FLOAT, mc_t2 FLOAT, liq_t2 FLOAT, volume_t2 FLOAT,
                    txns5m_t2 INT, buys_t2 INT, sells_t2 INT,
                    ratio_vol_mc_t2 FLOAT, var_t2 FLOAT, veredito_t2 TEXT,
                    p_t3 FLOAT, mc_t3 FLOAT, liq_t3 FLOAT, volume_t3 FLOAT,
                    txns5m_t3 INT, buys_t3 INT, sells_t3 INT,
                    ratio_vol_mc_t3 FLOAT, var_t3 FLOAT, veredito_t3 TEXT,
                    mc_pico         FLOAT,
                    var_pico        FLOAT,
                    categoria_final TEXT,
                    var_desde_compra FLOAT
                )
            """)
            cur.execute("CREATE INDEX IF NOT EXISTS idx_token_mint ON registros(token_mint)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_carteira ON registros(carteira)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_data ON registros(data_compra)")
            cur.execute("""
                CREATE TABLE IF NOT EXISTS signatures (
                    sig TEXT PRIMARY KEY
                )
            """)
        conn.commit()
    log("✅ Banco de dados inicializado")


def db_insert(reg):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO registros (
                    data_compra, carteira, tipo_carteira, token_mint, nome, dex,
                    fonte_dados, quantidade, signature, tipo, is_multi,
                    p_t0, mc_t0, liq_t0, volume_t0, txns5m_t0, buys_t0, sells_t0,
                    net_momentum_t0, idade_min, token_antigo, ratio_vol_mc_t0,
                    score_qualidade, holders_count, top1_pct, top10_pct,
                    dev_saiu, bc_progress, mc_pico, categoria_final,
                    var_desde_compra
                ) VALUES (
                    %(data_compra)s, %(carteira)s, %(tipo_carteira)s, %(token_mint)s,
                    %(nome)s, %(dex)s, %(fonte_dados)s, %(quantidade)s, %(signature)s,
                    %(tipo)s, %(is_multi)s, %(p_t0)s, %(mc_t0)s, %(liq_t0)s,
                    %(volume_t0)s, %(txns5m_t0)s, %(buys_t0)s, %(sells_t0)s,
                    %(net_momentum_t0)s, %(idade_min)s, %(token_antigo)s,
                    %(ratio_vol_mc_t0)s, %(score_qualidade)s, %(holders_count)s,
                    %(top1_pct)s, %(top10_pct)s, %(dev_saiu)s, %(bc_progress)s,
                    %(mc_pico)s, %(categoria_final)s, %(var_desde_compra)s
                ) RETURNING id
            """, reg)
            row_id = cur.fetchone()[0]
        conn.commit()
    return row_id


def db_update_checkpoint(row_id, checkpoint, preco, mc, liq, volume, txns, buys, sells, ratio, var, veredito, mc_pico):
    n = checkpoint  # t1, t2, t3
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(f"""
                UPDATE registros SET
                    p_{n}=%s, mc_{n}=%s, liq_{n}=%s, volume_{n}=%s,
                    txns5m_{n}=%s, buys_{n}=%s, sells_{n}=%s,
                    ratio_vol_mc_{n}=%s, var_{n}=%s, veredito_{n}=%s,
                    mc_pico=%s
                WHERE id=%s
            """, (preco, mc, liq, volume, txns, buys, sells, ratio, var, veredito, mc_pico, row_id))
        conn.commit()


def db_update_final(row_id, mc_pico, var_pico, categoria):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE registros SET mc_pico=%s, var_pico=%s, categoria_final=%s
                WHERE id=%s
            """, (mc_pico, var_pico, categoria, row_id))
        conn.commit()


def db_update_multi(row_id):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE registros SET is_multi=TRUE WHERE id=%s", (row_id,))
        conn.commit()


def db_carregar_estado():
    """Carrega dados do banco para memória ao iniciar."""
    try:
        with get_conn() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute("SELECT sig FROM signatures")
                for row in cur.fetchall():
                    signatures_vistas.add(row["sig"])

                cur.execute("SELECT * FROM registros ORDER BY data_compra")
                rows = cur.fetchall()

        log(f"📂 Carregando {len(rows)} registros do banco...")
        for row in rows:
            reg = dict(row)
            # Normaliza nomes de colunas para compatibilidade com código existente
            reg["var_t1_%"] = reg.pop("var_t1", None)
            reg["var_t2_%"] = reg.pop("var_t2", None)
            reg["var_t3_%"] = reg.pop("var_t3", None)
            reg["var_pico_%"] = reg.pop("var_pico", None)
            if reg.get("data_compra"):
                reg["data_compra"] = reg["data_compra"].strftime("%Y-%m-%d %H:%M:%S")

            nome = reg.get("carteira")
            if nome and nome in estado:
                idx = len(estado[nome]["registros"])
                estado[nome]["registros"].append(reg)
                estado[nome]["tokens_conhecidos"].add(reg["token_mint"])
                # Tokens ainda pendentes (sem categoria final ou aguardando)
                if reg.get("categoria_final") == "⏳ aguardando" and reg.get("tipo") == "COMPRA":
                    mint = reg["token_mint"]
                    db_id = reg["id"]
                    # Calcular quanto tempo passou desde a compra
                    try:
                        dt_compra = datetime.strptime(reg["data_compra"], "%Y-%m-%d %H:%M:%S")
                        segundos_passados = (datetime.now() - dt_compra).total_seconds()
                    except:
                        segundos_passados = 9999

                    # Se passou mais de 2 horas, finalizar como sem dados
                    if segundos_passados > 7200:
                        log(f"⚠️  Token preso há {segundos_passados/3600:.1f}h — finalizando: {reg.get('nome','?')}")
                        cat = "❓ DADOS INCOMPLETOS — restart perdeu checkpoints"
                        try:
                            db_update_final(db_id, reg.get("mc_pico") or reg.get("mc_t0") or 0, None, cat)
                            reg["categoria_final"] = cat
                        except Exception as e:
                            log(f"⚠️  Erro ao finalizar token preso: {e}")
                        continue  # não adiciona aos pendentes

                    # Reagendar checkpoints restantes
                    estado[nome]["pendentes"][mint] = {"idx": idx, "db_id": db_id}
                    # Checkpoints principais
                    if reg.get("mc_t1") is None:
                        delay = max(0, 300 - segundos_passados)
                        threading.Timer(delay, checar_checkpoint, args=[nome, mint, "t1"]).start()
                    if reg.get("mc_t2") is None:
                        delay = max(0, 900 - segundos_passados)
                        threading.Timer(delay, checar_checkpoint, args=[nome, mint, "t2"]).start()
                    if reg.get("mc_t3") is None:
                        delay = max(0, 2700 - segundos_passados)
                        threading.Timer(delay, checar_checkpoint, args=[nome, mint, "t3"]).start()
                    # Snapshots intermediários de pico
                    if segundos_passados < 120:
                        threading.Timer(max(0, 120 - segundos_passados), atualizar_pico, args=[nome, mint, "2min"]).start()
                    if segundos_passados < 600:
                        threading.Timer(max(0, 600 - segundos_passados), atualizar_pico, args=[nome, mint, "10min"]).start()
                    if segundos_passados < 1500:
                        threading.Timer(max(0, 1500 - segundos_passados), atualizar_pico, args=[nome, mint, "25min"]).start()

        log(f"✅ Estado restaurado — {sum(len(estado[n]['registros']) for n in estado)} registros em memória")
    except Exception as e:
        log(f"⚠️  Erro ao carregar estado do banco: {e}")


def db_sig_add(sig):
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("INSERT INTO signatures(sig) VALUES(%s) ON CONFLICT DO NOTHING", (sig,))
            conn.commit()
    except:
        pass


def log(msg):
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}", flush=True)


def telegram(msg):
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT, "text": msg, "parse_mode": "HTML"},
            timeout=10,
        )
        if r.status_code != 200:
            log(f"Telegram erro {r.status_code}: {r.text[:100]}")
    except Exception as e:
        log(f"Telegram erro: {e}")


def telegram_documento(caminho, caption=""):
    try:
        with open(caminho, "rb") as f:
            requests.post(
                f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendDocument",
                data={"chat_id": TELEGRAM_CHAT, "caption": caption, "parse_mode": "HTML"},
                files={"document": f},
                timeout=30,
            )
    except Exception as e:
        log(f"Telegram doc erro: {e}")


def calcular_momentum(buys, sells):
    buys = buys or 0
    sells = sells or 0
    total = buys + sells
    if total == 0:
        return None, 0
    net = buys - sells
    blocos = round(buys / total * 8)
    barra = "🟢" * blocos + "⬜" * (8 - blocos)
    sinal = "+" if net >= 0 else ""
    return f"{barra} {sinal}{net} ({buys}B / {sells}S)", net


def classificar_momentum(net, total):
    if not total: return ""
    if net >= 10:  return "🔥 Comprando forte"
    if net >= 5:   return "📈 Pressão compradora"
    if net >= 0:   return "➡️  Equilibrado"
    if net >= -5:  return "📉 Pressão vendedora"
    return "🧊 Vendendo forte"


def calcular_score(mc_t0, liq_t0, txns, ratio_vol_mc, idade_min, dex):
    score = 0
    if ratio_vol_mc and ratio_vol_mc >= 3:     score += 3
    elif ratio_vol_mc and ratio_vol_mc >= 1.5: score += 2
    elif ratio_vol_mc and ratio_vol_mc >= 1:   score += 1
    if txns and 100 <= txns <= 450:            score += 2
    elif txns and txns < 100:                  score += 1
    elif txns and txns > 500:                  score -= 2
    if liq_t0 == 0:                            score += 2
    if idade_min and idade_min <= 15:          score += 2
    elif idade_min and idade_min <= 30:        score += 1
    if dex == "pumpfun":                       score += 1
    if ratio_vol_mc and ratio_vol_mc < 0.8:    score -= 2
    score = max(0, min(10, score))
    if score >= 7:   return score, "🟢", "ALTA CONFIANÇA"
    elif score >= 4: return score, "🟡", "MODERADO"
    else:            return score, "🔴", "BAIXA CONFIANÇA"


def veredito_parcial(mc_anterior, mc_atual, tempo):
    if not mc_anterior or not mc_atual or mc_anterior == 0:
        return "❓ sem dados"
    var = (mc_atual - mc_anterior) / mc_anterior * 100
    if   var >  200: return f"🚀 +{var:.0f}% em {tempo} — EXPLOSIVO"
    elif var >   50: return f"📈 +{var:.0f}% em {tempo} — FORTE"
    elif var >   10: return f"📊 +{var:.0f}% em {tempo} — SUBINDO"
    elif var >  -10: return f"➡️  {var:.0f}% em {tempo} — ESTÁVEL"
    elif var >  -50: return f"📉 {var:.0f}% em {tempo} — FRAQUEJANDO"
    else:            return f"💀 {var:.0f}% em {tempo} — COLAPSANDO"


def categoria_final(reg):
    mc0 = reg.get("mc_t0") or 0
    mc1 = reg.get("mc_t1") or 0
    mc2 = reg.get("mc_t2") or 0
    mc3 = reg.get("mc_t3") or 0
    if mc0 == 0: return "❓ SEM DADOS"

    var_t1 = reg.get("var_t1_%")
    var_t2 = reg.get("var_t2_%")
    var_t3 = reg.get("var_t3_%")

    # Detectar morte após pico: T1 alto mas T2/T3 zerados ou muito negativos
    t2_morreu = mc2 == 0 or (var_t2 is not None and var_t2 < -70)
    t3_morreu = mc3 == 0 or (var_t3 is not None and var_t3 < -70)

    if var_t1 and var_t1 > 50 and t2_morreu:
        return "🎯 PUMP & DUMP — Morreu após T1"
    if var_t1 and var_t1 > 50 and mc3 > 0 and t3_morreu:
        return "🎯 PUMP & DUMP — Morreu após pico"

    pico = max(mc1, mc2, mc3)
    var_pico  = (pico - mc0) / mc0 * 100 if mc0 else 0
    var_final = (mc3  - mc0) / mc0 * 100 if mc0 and mc3 else None

    if   var_pico > 200 and var_final and var_final >  100: return "🏆 VENCEDOR — Subiu forte e manteve"
    elif var_pico > 200 and var_final and var_final <    0: return "🎯 PUMP & DUMP — Subiu e colapsou"
    elif var_pico >  50 and var_final and var_final >   20: return "📈 BOM TRADE — Crescimento sólido"
    elif var_pico >  50 and var_final and var_final <  -20: return "⚠️  ARMADILHA — Pico rápido e queda"
    elif var_final and var_final >  20:                     return "📊 CRESCIMENTO ESTÁVEL"
    elif var_final and var_final > -20:                     return "➡️  LATERAL — Pouco movimento"
    elif var_final is not None:                             return "💀 MORREU — Queda consistente"
    else:                                                   return "❓ DADOS INCOMPLETOS"


def get_dados_token(mint):
    preco = mc = liq = volume = 0
    dex = nome = "?"
    txns_5min = buys_5min = sells_5min = 0
    idade_min = None
    fonte = "dexscreener"
    try:
        r = requests.get(f"https://api.dexscreener.com/latest/dex/tokens/{mint}", timeout=8)
        pairs = r.json().get("pairs") or []
        if pairs:
            par        = sorted(pairs, key=lambda x: x.get("liquidity", {}).get("usd", 0) or 0, reverse=True)[0]
            preco      = float(par["priceUsd"]) if par.get("priceUsd") else None
            mc         = par.get("marketCap") or 0
            liq        = par.get("liquidity", {}).get("usd") or 0
            volume     = par.get("volume", {}).get("h24") or 0
            dex        = par.get("dexId", "?")
            nome       = par.get("baseToken", {}).get("name", "?")
            m5         = par.get("txns", {}).get("m5", {})
            buys_5min  = m5.get("buys", 0)
            sells_5min = m5.get("sells", 0)
            txns_5min  = buys_5min + sells_5min
            criado_ts  = par.get("pairCreatedAt")
            if criado_ts:
                idade_min = round((time.time() - criado_ts / 1000) / 60, 1)
            if mc > 0:
                return preco, mc, liq, volume, dex, nome, txns_5min, idade_min, fonte, buys_5min, sells_5min
    except:
        pass
    fonte = "pumpfun"
    dex   = "pumpfun"
    try:
        r = requests.post(
            f"https://mainnet.helius-rpc.com/?api-key={HELIUS_API_KEY}",
            json={"jsonrpc": "2.0", "id": 1, "method": "getAsset", "params": {"id": mint}},
            timeout=8,
        )
        if r.status_code == 200:
            asset     = r.json().get("result", {})
            nome      = asset.get("content", {}).get("metadata", {}).get("name", "?")
            criado_ts = asset.get("createdAt")
            if criado_ts:
                idade_min = round((time.time() - criado_ts) / 60, 1)
    except:
        pass
    try:
        r = requests.post(
            f"https://mainnet.helius-rpc.com/?api-key={HELIUS_API_KEY}",
            json={"jsonrpc": "2.0", "id": 1, "method": "getTokenSupply", "params": [mint]},
            timeout=8,
        )
        if r.status_code == 200:
            result      = r.json().get("result", {}).get("value", {})
            supply      = float(result.get("uiAmount", 0))
            sol_price   = get_sol_price()
            tokens_sold = max(0, 1_000_000_000 - supply)
            virtual_sol = 30 + (tokens_sold / 1_000_000_000) * 800
            preco_sol   = virtual_sol / (793_000_000 - tokens_sold) if tokens_sold < 793_000_000 else 0
            preco       = preco_sol * sol_price if sol_price else None
            mc          = round(preco * 1_000_000_000, 0) if preco else 0
            liq         = round(virtual_sol * sol_price, 0) if sol_price else 0
    except:
        pass
    return preco, mc, liq, volume, dex, nome, txns_5min, idade_min, fonte, buys_5min, sells_5min


def get_sol_price():
    try:
        r = requests.get(
            "https://api.coingecko.com/api/v3/simple/price?ids=solana&vs_currencies=usd",
            timeout=5,
        )
        return r.json().get("solana", {}).get("usd", 0)
    except:
        return 0


def get_holder_data(mint, liq_t0=0, dev_wallet=None):
    holders_count = top1_pct = top10_pct = dev_saiu = bc_progress = None
    if liq_t0 == 0:
        try:
            r = requests.get(
                f"https://frontend-api.pump.fun/coins/{mint}",
                timeout=8, headers={"User-Agent": "Mozilla/5.0"},
            )
            if r.status_code == 200:
                data          = r.json()
                holders_count = data.get("holder_count")
                bc_progress   = data.get("bonding_curve_progress")
                dev_wallet_bc = data.get("creator")
                if dev_wallet_bc:
                    try:
                        r2 = requests.get(
                            f"https://frontend-api.pump.fun/coins/{mint}/holders",
                            timeout=8, headers={"User-Agent": "Mozilla/5.0"},
                        )
                        if r2.status_code == 200:
                            holders_list = r2.json()
                            top_wallets  = [h.get("owner", "") for h in holders_list[:20]]
                            dev_saiu     = dev_wallet_bc not in top_wallets
                            total_supply = 1_000_000_000
                            # Endereços conhecidos de LP e bonding curve — excluir do cálculo
                            LP_ADDRESSES = {
                                "39azUYFWPz3VHgKCf3VChUwbpURdCHRxjWVowf5jUJjg",  # pump.fun bonding curve
                                "Ce6TQqeHC9p8KetsN6JsjHK7UTZk7nasjjnr7XxXp9F1", # raydium LP
                                "5Q544fKrFoe6tsEbD7S8EmxGTJYAKtTVhAW5Q5pge4j1", # raydium authority
                            }
                            holders_validos = [h for h in holders_list if h.get("owner","") not in LP_ADDRESSES]
                            if holders_validos:
                                top10_pct = round(sum(h.get("balance", 0) for h in holders_validos[:10]) / total_supply * 100, 1)
                                # top1 excluindo LP (para referência interna, não exibido)
                                top1_pct  = round(holders_validos[0].get("balance", 0) / total_supply * 100, 1)
                    except:
                        pass
        except Exception as e:
            log(f"pump.fun holder erro: {e}")
        return holders_count, top1_pct, top10_pct, dev_saiu, bc_progress
    try:
        r = requests.post(
            f"https://mainnet.helius-rpc.com/?api-key={HELIUS_API_KEY}",
            json={"jsonrpc": "2.0", "id": 1, "method": "getTokenSupply", "params": [mint]},
            timeout=8,
        )
        total_supply = 0
        if r.status_code == 200:
            total_supply = float(r.json().get("result", {}).get("value", {}).get("uiAmount", 0))
        if total_supply > 0:
            r2 = requests.post(
                f"https://mainnet.helius-rpc.com/?api-key={HELIUS_API_KEY}",
                json={"jsonrpc": "2.0", "id": 1, "method": "getTokenLargestAccounts", "params": [mint]},
                timeout=8,
            )
            if r2.status_code == 200:
                accounts = r2.json().get("result", {}).get("value", [])
                if accounts:
                    # Filtrar endereços de LP conhecidos
                    LP_KNOWN = {
                        "39azUYFWPz3VHgKCf3VChUwbpURdCHRxjWVowf5jUJjg",
                        "Ce6TQqeHC9p8KetsN6JsjHK7UTZk7nasjjnr7XxXp9F1",
                        "5Q544fKrFoe6tsEbD7S8EmxGTJYAKtTVhAW5Q5pge4j1",
                        "HVh6wHNBAsG3pq1Bj5oCzRjoWKVogEDHwUHkRz3ekFgt",  # raydium pool
                    }
                    accs_validos = [a for a in accounts if a.get("address","") not in LP_KNOWN]
                    holders_count = len(accs_validos)
                    if accs_validos:
                        top10_pct = round(sum(float(a.get("uiAmount", 0)) for a in accs_validos[:10]) / total_supply * 100, 1)
                        top1_pct  = round(float(accs_validos[0].get("uiAmount", 0)) / total_supply * 100, 1)
                    if dev_wallet:
                        dev_saiu = dev_wallet not in [a.get("address", "") for a in accs_validos]
    except Exception as e:
        log(f"helius holder erro: {e}")
    return holders_count, top1_pct, top10_pct, dev_saiu, bc_progress


def extrair_mudancas_token(tx, carteira_addr):
    mudancas = {}
    for conta in tx.get("accountData", []):
        for change in conta.get("tokenBalanceChanges", []):
            if change.get("userAccount") != carteira_addr:
                continue
            mint = change.get("mint", "")
            if not mint or mint in TOKENS_IGNORAR:
                continue
            raw = change.get("rawTokenAmount", {})
            try:
                amount = int(raw.get("tokenAmount", "0")) / (10 ** int(raw.get("decimals", 0)))
            except:
                continue
            if amount != 0:
                mudancas[mint] = mudancas.get(mint, 0) + amount
    for transfer in tx.get("tokenTransfers", []):
        mint     = transfer.get("mint", "")
        to_acc   = transfer.get("toUserAccount", "")
        from_acc = transfer.get("fromUserAccount", "")
        if not mint or mint in TOKENS_IGNORAR:
            continue
        try:
            amount = float(transfer.get("tokenAmount", 0))
        except:
            continue
        if amount == 0:
            continue
        if to_acc == carteira_addr:
            mudancas[mint] = mudancas.get(mint, 0) + amount
        elif from_acc == carteira_addr:
            mudancas[mint] = mudancas.get(mint, 0) - amount
    return [{"mint": m, "amount": a} for m, a in mudancas.items()]


def checar_multi_carteira(mint, nome_token, carteira_atual, mc_t0, liq_t0,
                           ratio_vol_mc, idade_min, score, score_emoji, score_desc,
                           holders_count=None, top1_pct=None, top10_pct=None,
                           dev_saiu=None, bc_progress=None,
                           buys_5min=0, sells_5min=0):
    agora = time.time()
    if mint not in mints_globais:
        mints_globais[mint] = {}
    mints_globais[mint][carteira_atual] = agora

    recentes = {
        c: ts for c, ts in mints_globais[mint].items()
        if c != carteira_atual and (agora - ts) / 60 <= 60
    }
    if not recentes:
        return False

    timing_s = min(int(agora - ts) for ts in recentes.values())
    if timing_s < 120:
        timing_str = f"⚡ {timing_s}s"
        urgencia   = "🚨🚨 SINCRONIZADO"
    elif timing_s < 600:
        timing_str = f"~{timing_s//60}min"
        urgencia   = "🚨 MULTI-CARTEIRA"
    else:
        timing_str = f"{timing_s//60}min"
        urgencia   = "ℹ️ MULTI-CARTEIRA"

    todas   = list(recentes.keys()) + [carteira_atual]
    humanos = [c for c in todas if TIPO_CARTEIRA.get(c) == "humano"]
    if humanos:
        urgencia = "⭐" * len(humanos) + " " + urgencia

    def label(c):
        i = "👤" if TIPO_CARTEIRA.get(c) == "humano" else "🤖"
        return f"{i} <b>{c}</b>"

    linhas = [f"  • {label(c)} comprou há {round((agora-ts)/60,1)} min" for c, ts in recentes.items()]

    holder_linha = ""
    if holders_count:         holder_linha += f"\n👥 Holders: <b>{holders_count}</b>"
    if top1_pct is not None:  holder_linha += f" | Top: <b>{top1_pct}%</b>"
    if top10_pct is not None: holder_linha += f" | Top10: <b>{top10_pct}%</b>"
    if dev_saiu is True:      holder_linha += "\n✅ Dev saiu"
    elif dev_saiu is False:   holder_linha += "\n⚠️ Dev ainda segura"
    if bc_progress is not None: holder_linha += f"\n📈 BC: <b>{bc_progress:.0f}%</b>"

    momentum_linha = ""
    barra, net = calcular_momentum(buys_5min, sells_5min)
    if barra:
        momentum_linha = f"\n🔄 {barra}\n    {classificar_momentum(net, buys_5min + sells_5min)}"

    icone = "👤" if TIPO_CARTEIRA.get(carteira_atual) == "humano" else "🤖"

    telegram(
        f"{urgencia}\n\n"
        f"Token: <b>{nome_token}</b>\n"
        f"Mint: <code>{mint}</code>\n\n"
        f"{icone} <b>{carteira_atual}</b> comprou agora\n"
        + "\n".join(linhas) + "\n\n"
        f"⏱ Timing: <b>{timing_str}</b>\n\n"
        f"💰 MC: <b>${mc_t0:,.0f}</b>\n"
        f"💧 Liq: <b>${liq_t0:,.0f}</b>\n"
        f"📊 Vol/MC: <b>{ratio_vol_mc:.1f}x</b>\n"
        f"🕐 Idade: <b>{idade_min:.0f} min</b>"
        f"{holder_linha}"
        f"{momentum_linha}\n\n"
        f"Score: {score_emoji} <b>{score}/10 — {score_desc}</b>\n\n"
        f"🔗 https://pump.fun/{mint}"
    )
    log(f"🚨 MULTI: {nome_token} | {carteira_atual} + {list(recentes.keys())} | {timing_str}")
    return True


def processar_venda(carteira_addr, nome, mint, amount_vendido, tx):
    est = estado[nome]
    reg = next((r for r in est["registros"] if r.get("token_mint") == mint), None)
    _, mc_atual, _, _, _, nome_token, _, _, _, _, _ = get_dados_token(mint)
    nome_token = reg["nome"] if reg else nome_token
    variacao = None
    if reg and reg.get("p_t0"):
        preco_atual, _, _, _, _, _, _, _, _, _, _ = get_dados_token(mint)
        if preco_atual:
            variacao = round((preco_atual - reg["p_t0"]) / reg["p_t0"] * 100, 2)
    log(f"🔴 [{nome}] VENDA: {nome_token} | MC: ${mc_atual:,.0f} | variação: {f'{variacao:+.1f}%' if variacao is not None else '—'}")
    data = datetime.fromtimestamp(tx.get("timestamp", time.time())).strftime("%Y-%m-%d %H:%M:%S")
    reg_venda = {
        "data_compra": data, "carteira": nome, "tipo_carteira": TIPO_CARTEIRA.get(nome, "?"),
        "token_mint": mint, "nome": nome_token, "dex": "venda", "fonte_dados": "venda",
        "quantidade": round(abs(amount_vendido), 4), "signature": tx.get("signature", ""),
        "tipo": "VENDA", "is_multi": False,
        "p_t0": None, "mc_t0": mc_atual, "liq_t0": None, "volume_t0": None,
        "txns5m_t0": None, "buys_t0": None, "sells_t0": None, "net_momentum_t0": None,
        "idade_min": None, "token_antigo": None, "ratio_vol_mc_t0": None,
        "score_qualidade": None, "holders_count": None, "top1_pct": None,
        "top10_pct": None, "dev_saiu": None, "bc_progress": None,
        "mc_pico": None, "categoria_final": "🔴 VENDA", "var_desde_compra": variacao,
    }
    est["registros"].append(reg_venda)
    try:
        db_insert(reg_venda)
    except Exception as e:
        log(f"⚠️  DB insert venda erro: {e}")


def agendar_checkpoints(nome, mint):
    # Checkpoints principais — gravam dados no banco e dashboard
    threading.Timer(5  * 60, checar_checkpoint, args=[nome, mint, "t1"]).start()
    threading.Timer(15 * 60, checar_checkpoint, args=[nome, mint, "t2"]).start()
    threading.Timer(45 * 60, checar_checkpoint, args=[nome, mint, "t3"]).start()
    # Snapshots intermediários — só atualizam mc_pico se for maior
    threading.Timer(2  * 60, atualizar_pico, args=[nome, mint, "2min"]).start()
    threading.Timer(10 * 60, atualizar_pico, args=[nome, mint, "10min"]).start()
    threading.Timer(25 * 60, atualizar_pico, args=[nome, mint, "25min"]).start()


def atualizar_pico(nome, mint, label):
    """Verifica o MC atual e atualiza mc_pico se for maior — sem alterar checkpoints."""
    est = estado[nome]
    if mint not in est["pendentes"]:
        return
    info = est["pendentes"][mint]
    reg  = est["registros"][info["idx"]]
    db_id = info.get("db_id")
    try:
        _, mc_atual, _, _, _, _, _, _, _, _, _ = get_dados_token(mint)
        if not mc_atual or mc_atual == 0:
            return
        mc_pico_atual = reg.get("mc_pico") or 0
        if mc_atual > mc_pico_atual:
            reg["mc_pico"] = mc_atual
            log(f"  📈 [{nome}] Pico atualizado {label}: {reg['nome'][:20]} | MC: ${mc_atual:,.0f} (era ${mc_pico_atual:,.0f})")
            if db_id:
                with get_conn() as conn:
                    with conn.cursor() as cur:
                        cur.execute("UPDATE registros SET mc_pico=%s WHERE id=%s", (mc_atual, db_id))
                    conn.commit()
    except Exception as e:
        log(f"⚠️  atualizar_pico erro [{label}]: {e}")


def checar_checkpoint(nome, mint, checkpoint):
    est = estado[nome]
    if mint not in est["pendentes"]:
        return
    info  = est["pendentes"][mint]
    reg   = est["registros"][info["idx"]]
    db_id = info.get("db_id")
    preco, mc, liq, volume, _, _, txns_5min, _, _, buys, sells = get_dados_token(mint)
    ratio = round(volume / reg["mc_t0"], 2) if reg.get("mc_t0", 0) > 0 else None

    if checkpoint == "t1":
        var_t1 = round((preco - reg["p_t0"]) / reg["p_t0"] * 100, 2) if preco and reg.get("p_t0") else None
        veredito = veredito_parcial(reg["mc_t0"], mc, "5min")
        mc_pico = max(mc, reg.get("mc_pico") or 0)
        reg.update({
            "p_t1": preco, "mc_t1": mc, "liq_t1": liq, "volume_t1": volume,
            "txns5m_t1": txns_5min, "buys_t1": buys, "sells_t1": sells,
            "ratio_vol_mc_t1": ratio, "var_t1_%": var_t1,
            "veredito_t1": veredito, "mc_pico": mc_pico,
        })
        if db_id:
            db_update_checkpoint(db_id, "t1", preco, mc, liq, volume, txns_5min, buys, sells, ratio, var_t1, veredito, mc_pico)
        log(f"  ⏱️  [{nome}] T1 {reg['nome'][:20]} | MC: ${mc:,.0f} | {veredito}")
        if reg.get("is_multi") and var_t1:
            if var_t1 >= 100:
                telegram(f"🚨 <b>SAÍDA — T1 EXPLOSIVO</b>\n\nToken: <b>{reg['nome']}</b>\n📈 T1: <b>+{var_t1:.0f}%</b> em 5min\n💰 MC: <b>${mc:,.0f}</b>\n\n⚠️ <i>Considere realizar lucro.</i>\n\n🔗 https://pump.fun/{reg['token_mint']}")
            elif var_t1 >= 50:
                telegram(f"⚠️ <b>SAÍDA — T1 FORTE</b>\n\nToken: <b>{reg['nome']}</b>\n📈 T1: <b>+{var_t1:.0f}%</b> em 5min\n💰 MC: <b>${mc:,.0f}</b>\n\n💡 <i>Considere realizar parte.</i>\n\n🔗 https://pump.fun/{reg['token_mint']}")

    elif checkpoint == "t2":
        var_t2 = round((preco - reg["p_t0"]) / reg["p_t0"] * 100, 2) if preco and reg.get("p_t0") else None
        veredito = veredito_parcial(reg.get("mc_t1"), mc, "15min")
        mc_pico = max(mc, reg.get("mc_pico") or 0)
        reg.update({
            "p_t2": preco, "mc_t2": mc, "liq_t2": liq, "volume_t2": volume,
            "txns5m_t2": txns_5min, "buys_t2": buys, "sells_t2": sells,
            "ratio_vol_mc_t2": ratio, "var_t2_%": var_t2,
            "veredito_t2": veredito, "mc_pico": mc_pico,
        })
        if db_id:
            db_update_checkpoint(db_id, "t2", preco, mc, liq, volume, txns_5min, buys, sells, ratio, var_t2, veredito, mc_pico)
        log(f"  ⏱️  [{nome}] T2 {reg['nome'][:20]} | MC: ${mc:,.0f} | {veredito}")

    elif checkpoint == "t3":
        var_t3 = round((preco - reg["p_t0"]) / reg["p_t0"] * 100, 2) if preco and reg.get("p_t0") else None
        veredito = veredito_parcial(reg.get("mc_t2"), mc, "45min")
        mc_pico = max(mc, reg.get("mc_pico") or 0)
        var_pico = round((mc_pico - reg["mc_t0"]) / reg["mc_t0"] * 100, 2) if reg.get("mc_t0") else None
        cat = categoria_final({**reg, "mc_t3": mc})
        reg.update({
            "p_t3": preco, "mc_t3": mc, "liq_t3": liq, "volume_t3": volume,
            "txns5m_t3": txns_5min, "buys_t3": buys, "sells_t3": sells,
            "ratio_vol_mc_t3": ratio, "var_t3_%": var_t3,
            "veredito_t3": veredito, "mc_pico": mc_pico,
            "var_pico_%": var_pico, "categoria_final": cat,
        })
        if db_id:
            db_update_checkpoint(db_id, "t3", preco, mc, liq, volume, txns_5min, buys, sells, ratio, var_t3, veredito, mc_pico)
            db_update_final(db_id, mc_pico, var_pico, cat)
        log(f"  ✅ [{nome}] FINAL {reg['nome'][:20]} | MC: ${mc:,.0f} | {cat}")
        del est["pendentes"][mint]


def processar_tx(tx, carteira_addr, nome):
    est = estado[nome]
    if tx.get("type") == "TRANSFER" and tx.get("source") == "SYSTEM_PROGRAM":
        return
    for mudanca in extrair_mudancas_token(tx, carteira_addr):
        mint   = mudanca["mint"]
        amount = mudanca["amount"]
        if amount == 0:
            continue
        if amount < 0:
            processar_venda(carteira_addr, nome, mint, amount, tx)
            continue
        if mint in est["tokens_conhecidos"]:
            continue
        est["tokens_conhecidos"].add(mint)

        data = datetime.fromtimestamp(tx.get("timestamp", time.time())).strftime("%Y-%m-%d %H:%M:%S")
        preco_t0, mc_t0, liq_t0, volume_t0, dex, nome_token, txns_5min, idade_min, fonte, buys_5min, sells_5min = get_dados_token(mint)
        ratio_vol_mc_t0 = round(volume_t0 / mc_t0, 2) if mc_t0 > 0 else None
        token_antigo    = "sim" if (idade_min and idade_min > 1440) else "não"
        score, score_emoji, score_desc = calcular_score(mc_t0, liq_t0, txns_5min, ratio_vol_mc_t0, idade_min, dex)

        holders_count = top1_pct = top10_pct = dev_saiu = bc_progress = None
        try:
            holders_count, top1_pct, top10_pct, dev_saiu, bc_progress = get_holder_data(mint, liq_t0=liq_t0)
        except Exception as e:
            log(f"holders erro [{nome_token}]: {e}")

        flag_antigo = f" ⚠️ TOKEN ANTIGO ({idade_min/1440:.0f}d)" if token_antigo == "sim" else ""
        # Token sem MC — provavelmente morreu antes de ser indexado
        if not mc_t0 or mc_t0 == 0:
            log(f"⚠️  [{nome}] {nome_token} | MC=0 — token não indexado, ignorando checkpoints")
            reg_sem_dados = {
                "data_compra": data, "carteira": nome, "tipo_carteira": TIPO_CARTEIRA.get(nome, "?"),
                "token_mint": mint, "nome": nome_token, "dex": dex, "fonte_dados": fonte,
                "quantidade": round(amount, 4), "signature": tx.get("signature", ""),
                "tipo": "COMPRA", "is_multi": False,
                "p_t0": None, "mc_t0": 0, "liq_t0": liq_t0, "volume_t0": volume_t0,
                "txns5m_t0": txns_5min, "buys_t0": buys_5min, "sells_t0": sells_5min,
                "net_momentum_t0": 0, "idade_min": idade_min, "token_antigo": token_antigo,
                "ratio_vol_mc_t0": None, "score_qualidade": 0,
                "holders_count": None, "top1_pct": None, "top10_pct": None,
                "dev_saiu": None, "bc_progress": None,
                "p_t1": None, "mc_t1": None, "liq_t1": None, "volume_t1": None,
                "txns5m_t1": None, "buys_t1": None, "sells_t1": None,
                "ratio_vol_mc_t1": None, "var_t1_%": None, "veredito_t1": None,
                "p_t2": None, "mc_t2": None, "liq_t2": None, "volume_t2": None,
                "txns5m_t2": None, "buys_t2": None, "sells_t2": None,
                "ratio_vol_mc_t2": None, "var_t2_%": None, "veredito_t2": None,
                "p_t3": None, "mc_t3": None, "liq_t3": None, "volume_t3": None,
                "txns5m_t3": None, "buys_t3": None, "sells_t3": None,
                "ratio_vol_mc_t3": None, "var_t3_%": None, "veredito_t3": None,
                "mc_pico": 0, "var_pico_%": None, "var_desde_compra": None,
                "categoria_final": "❓ SEM DADOS — MC não disponível",
            }
            est["registros"].append(reg_sem_dados)
            try:
                db_insert(reg_sem_dados)
            except Exception as e:
                log(f"⚠️  DB insert sem dados erro: {e}")
            continue

        log(f"🆕 [{nome}] {nome_token} | {dex} | MC: ${mc_t0:,.0f} | Score: {score}/10{flag_antigo}")

        reg = {
            "data_compra": data, "carteira": nome, "tipo_carteira": TIPO_CARTEIRA.get(nome, "?"),
            "token_mint": mint, "nome": nome_token, "dex": dex, "fonte_dados": fonte,
            "quantidade": round(amount, 4), "signature": tx.get("signature", ""),
            "tipo": "COMPRA", "is_multi": False,
            "p_t0": preco_t0, "mc_t0": mc_t0, "liq_t0": liq_t0, "volume_t0": volume_t0,
            "txns5m_t0": txns_5min, "buys_t0": buys_5min, "sells_t0": sells_5min,
            "net_momentum_t0": (buys_5min or 0) - (sells_5min or 0),
            "idade_min": idade_min, "token_antigo": token_antigo,
            "ratio_vol_mc_t0": ratio_vol_mc_t0, "score_qualidade": score,
            "holders_count": holders_count, "top1_pct": top1_pct,
            "top10_pct": top10_pct, "dev_saiu": dev_saiu, "bc_progress": bc_progress,
            "p_t1": None, "mc_t1": None, "liq_t1": None, "volume_t1": None,
            "txns5m_t1": None, "buys_t1": None, "sells_t1": None,
            "ratio_vol_mc_t1": None, "var_t1_%": None, "veredito_t1": None,
            "p_t2": None, "mc_t2": None, "liq_t2": None, "volume_t2": None,
            "txns5m_t2": None, "buys_t2": None, "sells_t2": None,
            "ratio_vol_mc_t2": None, "var_t2_%": None, "veredito_t2": None,
            "p_t3": None, "mc_t3": None, "liq_t3": None, "volume_t3": None,
            "txns5m_t3": None, "buys_t3": None, "sells_t3": None,
            "ratio_vol_mc_t3": None, "var_t3_%": None, "veredito_t3": None,
            "mc_pico": mc_t0, "var_pico_%": None, "var_desde_compra": None,
            "categoria_final": "⏳ aguardando",
        }

        idx = len(est["registros"])
        est["registros"].append(reg)

        db_id = None
        try:
            db_id = db_insert(reg)
        except Exception as e:
            log(f"⚠️  DB insert erro: {e}")

        est["pendentes"][mint] = {"idx": idx, "db_id": db_id}

        is_multi = checar_multi_carteira(
            mint, nome_token, nome, mc_t0, liq_t0,
            ratio_vol_mc_t0 or 0, idade_min or 0,
            score, score_emoji, score_desc,
            holders_count=holders_count, top1_pct=top1_pct,
            top10_pct=top10_pct, dev_saiu=dev_saiu, bc_progress=bc_progress,
            buys_5min=buys_5min, sells_5min=sells_5min,
        )
        est["registros"][idx]["is_multi"] = bool(is_multi)
        if is_multi and db_id:
            try:
                db_update_multi(db_id)
            except:
                pass

        agendar_checkpoints(nome, mint)


def enviar_csv_diario():
    log("📤 Enviando CSV diário...")
    todos = []
    for nome in set(CARTEIRAS.values()):
        todos.extend(estado[nome]["registros"])
    agora = datetime.now().strftime("%d/%m/%Y %H:%M")
    if not todos:
        telegram("📊 <b>Relatório diário</b>\n\nNenhum registro ainda.")
        threading.Timer(24 * 60 * 60, enviar_csv_diario).start()
        return
    caminho = "monitoramento_consolidado.csv"
    pd.DataFrame(todos).to_csv(caminho, index=False)
    compras = sum(1 for r in todos if r.get("tipo") == "COMPRA")
    vendas  = sum(1 for r in todos if r.get("tipo") == "VENDA")
    multis  = sum(1 for r in todos if r.get("is_multi"))
    telegram_documento(caminho, caption=(
        f"📊 <b>Relatório consolidado</b>\nGerado em: {agora}\n\n"
        f"Compras: <b>{compras}</b> | Vendas: <b>{vendas}</b>\nMulti: <b>{multis}</b>"
    ))
    threading.Timer(24 * 60 * 60, enviar_csv_diario).start()


# ══════════════════════════════════════════════════════════
# ROTAS
# ══════════════════════════════════════════════════════════
@app.route("/webhook", methods=["POST"])
def webhook():
    try:
        txs = request.get_json()
        if not txs:
            return jsonify({"ok": True})
        for tx in txs:
            sig = tx.get("signature", "")
            if sig in signatures_vistas:
                continue
            if sig:
                signatures_vistas.add(sig)
                threading.Thread(target=db_sig_add, args=[sig], daemon=True).start()
            for acc in tx.get("accountData", []):
                addr = acc.get("account", "")
                if addr in CARTEIRAS:
                    processar_tx(tx, addr, CARTEIRAS[addr])
                    break
        return jsonify({"ok": True})
    except Exception as e:
        import traceback
        log(f"Webhook erro: {e}\n{traceback.format_exc()}")
        return jsonify({"ok": False}), 500


@app.route("/", methods=["GET"])
def health():
    total   = sum(len(estado[n]["registros"]) for n in estado)
    pend    = sum(len(estado[n]["pendentes"]) for n in estado)
    compras = sum(1 for n in estado for r in estado[n]["registros"] if r.get("tipo") == "COMPRA")
    vendas  = sum(1 for n in estado for r in estado[n]["registros"] if r.get("tipo") == "VENDA")
    multis  = sum(1 for n in estado for r in estado[n]["registros"] if r.get("is_multi"))
    return jsonify({
        "status": "running v6.3+db", "registros": total,
        "compras": compras, "vendas": vendas,
        "multis": multis, "pendentes": pend,
    })


@app.route("/dados", methods=["GET"])
def dados():
    if request.args.get("key") != DASHBOARD_KEY:
        return jsonify({"erro": "nao autorizado"}), 401

    todos = []
    for n in estado:
        todos.extend(estado[n]["registros"])
    todos_sorted = sorted(todos, key=lambda r: r.get("data_compra", ""), reverse=True)

    ativos = []
    for n in estado:
        for mint, info in estado[n]["pendentes"].items():
            ativos.append(dict(estado[n]["registros"][info["idx"]]))

    multis = [r for r in todos_sorted if r.get("is_multi") and r.get("tipo") == "COMPRA"][:50]

    stats = {}
    for n in set(CARTEIRAS.values()):
        regs_n      = [r for r in todos if r.get("carteira") == n and r.get("tipo") == "COMPRA"]
        finalizados = [r for r in regs_n if r.get("categoria_final") and "aguardando" not in r.get("categoria_final", "")]
        vencedores  = [r for r in finalizados if r.get("var_pico_%") and r["var_pico_%"] > 20]
        winrate     = round(len(vencedores) / len(finalizados) * 100, 1) if finalizados else 0
        stats[n] = {
            "tipo": TIPO_CARTEIRA.get(n, "?"),
            "total": len(regs_n),
            "finalizados": len(finalizados),
            "vencedores": len(vencedores),
            "winrate": winrate,
        }

    return jsonify({
        "status":    "ok",
        "versao":    "v6.3+db",
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "resumo": {
            "total_registros": len(todos),
            "compras": sum(1 for r in todos if r.get("tipo") == "COMPRA"),
            "vendas":  sum(1 for r in todos if r.get("tipo") == "VENDA"),
            "multis":  len(multis),
            "ativos":  len(ativos),
        },
        "stats_carteiras": stats,
        "tokens_ativos":   ativos,
        "alertas_multi":   multis,
        "historico":       [r for r in todos_sorted if r.get("categoria_final") and "aguardando" not in r.get("categoria_final", "")][:200],
    })


# ══════════════════════════════════════════════════════════
# STARTUP
# ══════════════════════════════════════════════════════════
def startup():
    time.sleep(3)
    init_db()
    db_carregar_estado()
    total = sum(len(estado[n]["registros"]) for n in estado)
    telegram(
        f"🚀 <b>Monitor v6.3 + PostgreSQL iniciado!</b>\n\n"
        f"📂 {total} registros restaurados do banco\n\n"
        "🤖 carteira_A | 🤖 carteira_B\n"
        "👤 carteira_C | 👤 carteira_D"
    )
    log("✅ Monitor v6.3+db — aguardando transações")
    threading.Timer(24 * 60 * 60, enviar_csv_diario).start()


if __name__ == "__main__":
    log("🚀 MONITOR v6.3+DB INICIADO")
    for addr, nome in CARTEIRAS.items():
        log(f"   {nome}: {addr[:20]}...")
    threading.Thread(target=startup, daemon=True).start()
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
