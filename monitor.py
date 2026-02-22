import requests
import pandas as pd
import time
from datetime import datetime, timezone

HELIUS_API_KEY = "6ccebda4-8501-4224-a238-03d909a0d893"

CARTEIRAS = {
    "carteira_A": "GijFWw4oNyh9ko3FaZforNsi3jk6wDovARpkKahPD4o5",
    "carteira_B": "ANfB2knFb7pC7jKadHnSP4xKZ31KJGNLhWRo89LWsFeW",
    "carteira_C": "43C9gHfJ7YgqKv5ft3hodFgumydv1nEiNHD1PuANufk5",
}

INTERVALO_VERIFICACAO = 30   # segundos
SALVAR_A_CADA        = 10   # minutos

TOKENS_IGNORAR = {
    "So11111111111111111111111111111111111111112",
    "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
    "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB",
    "8S4Hk9bMLTTCBzBrFGSRcPbHiWbVXKpmWHvEMPEELXXt",
    "11111111111111111111111111111111",
}

estado = {
    nome: {
        "tokens_conhecidos": set(),
        "registros":         [],
        "pendentes":         {},
        "ultimo_save":       time.time(),
        "arquivo_csv":       f"monitoramento_{nome}.csv",
    }
    for nome in CARTEIRAS
}

def log(msg):
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}", flush=True)

# ── VEREDITO PARCIAL ──────────────────────────────────────
def veredito_parcial(mc_anterior, mc_atual, tempo):
    if not mc_anterior or not mc_atual or mc_anterior == 0:
        return "❓ sem dados"
    var = (mc_atual - mc_anterior) / mc_anterior * 100
    if var > 200:   return f"🚀 +{var:.0f}% em {tempo} — EXPLOSIVO"
    elif var > 50:  return f"📈 +{var:.0f}% em {tempo} — FORTE"
    elif var > 10:  return f"📊 +{var:.0f}% em {tempo} — SUBINDO"
    elif var > -10: return f"➡️  {var:.0f}% em {tempo} — ESTÁVEL"
    elif var > -50: return f"📉 {var:.0f}% em {tempo} — FRAQUEJANDO"
    else:           return f"💀 {var:.0f}% em {tempo} — COLAPSANDO"

def categoria_final(reg):
    mc0 = reg.get("mc_t0") or 0
    mc1 = reg.get("mc_t1") or 0
    mc2 = reg.get("mc_t2") or 0
    mc3 = reg.get("mc_t3") or 0

    if mc0 == 0:
        return "❓ SEM DADOS SUFICIENTES"

    pico     = max(mc1, mc2, mc3)
    var_pico = (pico - mc0) / mc0 * 100 if mc0 > 0 else 0
    var_final = (mc3 - mc0) / mc0 * 100 if mc0 > 0 and mc3 > 0 else None

    if var_pico > 200 and var_final and var_final > 100:
        return "🏆 VENCEDOR — Subiu forte e manteve"
    elif var_pico > 200 and var_final and var_final < 0:
        return "🎯 PUMP & DUMP — Subiu e colapsou"
    elif var_pico > 50 and var_final and var_final > 20:
        return "📈 BOM TRADE — Crescimento sólido"
    elif var_pico > 50 and var_final and var_final < -20:
        return "⚠️  ARMADILHA — Pico rápido e queda"
    elif var_final and var_final > 20:
        return "📊 CRESCIMENTO ESTÁVEL"
    elif var_final and var_final > -20:
        return "➡️  LATERAL — Pouco movimento"
    elif var_final is not None:
        return "💀 MORREU — Queda consistente"
    else:
        return "❓ DADOS INCOMPLETOS"

# ── APIs ──────────────────────────────────────────────────
def get_ultimas_txs(carteira):
    url = f"https://api.helius.xyz/v0/addresses/{carteira}/transactions?api-key={HELIUS_API_KEY}"
    try:
        r = requests.get(url, params={"limit": 10}, timeout=15)
        return r.json() if r.status_code == 200 else []
    except Exception as e:
        log(f"⚠️  Erro ao buscar txs: {e}")
        return []

def get_dados_token(mint):
    preco, mc, liq, volume, dex, nome, holders = None, 0, 0, 0, "?", "?", 0

    # DexScreener — preço, MC, liquidez, volume, txns como proxy de holders
    try:
        r = requests.get(
            f"https://api.dexscreener.com/latest/dex/tokens/{mint}",
            timeout=8
        )
        pairs = r.json().get("pairs") or []
        if pairs:
            par    = sorted(pairs, key=lambda x: x.get("liquidity", {}).get("usd", 0) or 0, reverse=True)[0]
            preco  = float(par["priceUsd"]) if par.get("priceUsd") else None
            mc     = par.get("marketCap") or 0
            liq    = par.get("liquidity", {}).get("usd") or 0
            volume = par.get("volume", {}).get("h24") or 0
            dex    = par.get("dexId", "?")
            nome   = par.get("baseToken", {}).get("name", "?")
            # txns nas últimas 24h como proxy de participantes únicos
            txns   = par.get("txns", {}).get("h24", {})
            holders = txns.get("buys", 0) + txns.get("sells", 0)
    except:
        pass

    return preco, mc, liq, volume, dex, nome, holders

def extrair_novas_compras(txs, carteira_addr, tokens_conhecidos):
    novas = []
    for tx in txs:
        tipo = tx.get("type", "")
        src  = tx.get("source", "")
        sig  = tx.get("signature", "")
        ts   = tx.get("timestamp", 0)

        if tipo == "TRANSFER" and src == "SYSTEM_PROGRAM":
            continue

        for conta in tx.get("accountData", []):
            for change in conta.get("tokenBalanceChanges", []):
                if change.get("userAccount") != carteira_addr:
                    continue
                mint = change.get("mint", "")
                if not mint or mint in TOKENS_IGNORAR:
                    continue
                raw = change.get("rawTokenAmount", {})
                try:
                    amount = int(raw.get("tokenAmount", "0")) / (10 ** raw.get("decimals", 0))
                except:
                    continue
                if amount > 0 and mint not in tokens_conhecidos:
                    novas.append({
                        "mint":       mint,
                        "timestamp":  ts,
                        "signature":  sig,
                        "quantidade": round(amount, 4),
                    })
                    tokens_conhecidos.add(mint)
    return novas

# ── CHECAR PENDENTES ──────────────────────────────────────
def checar_pendentes(nome):
    est   = estado[nome]
    agora = time.time()

    for mint, info in list(est["pendentes"].items()):
        ts  = info["ts_compra"]
        idx = info["idx"]
        reg = est["registros"][idx]

        # T1 — 5 minutos
        if not info["t1_ok"] and agora >= ts + 5 * 60:
            preco, mc, liq, volume, _, _, holders = get_dados_token(mint)
            reg["p_t1"]       = preco
            reg["mc_t1"]      = mc
            reg["liq_t1"]     = liq
            reg["volume_t1"]  = volume
            reg["holders_t1"] = holders
            if preco and reg["p_t0"]:
                reg["var_t1_%"] = round((preco - reg["p_t0"]) / reg["p_t0"] * 100, 2)
            reg["veredito_t1"] = veredito_parcial(reg["mc_t0"], mc, "5min")
            info["t1_ok"] = True
            log(f"  ⏱️  [{nome}] T1 {reg['nome'][:18]} | "
                f"MC: ${mc:,.0f} | Vol: ${volume:,.0f} | "
                f"Txns24h: {holders} | {reg['veredito_t1']}")

        # T2 — 15 minutos
        if not info["t2_ok"] and agora >= ts + 15 * 60:
            preco, mc, liq, volume, _, _, holders = get_dados_token(mint)
            reg["p_t2"]       = preco
            reg["mc_t2"]      = mc
            reg["liq_t2"]     = liq
            reg["volume_t2"]  = volume
            reg["holders_t2"] = holders
            if preco and reg["p_t0"]:
                reg["var_t2_%"] = round((preco - reg["p_t0"]) / reg["p_t0"] * 100, 2)
            reg["veredito_t2"] = veredito_parcial(reg["mc_t1"], mc, "15min")
            info["t2_ok"] = True
            log(f"  ⏱️  [{nome}] T2 {reg['nome'][:18]} | "
                f"MC: ${mc:,.0f} | Vol: ${volume:,.0f} | "
                f"Txns24h: {holders} | {reg['veredito_t2']}")

        # T3 — 45 minutos
        if not info["t3_ok"] and agora >= ts + 45 * 60:
            preco, mc, liq, volume, _, _, holders = get_dados_token(mint)
            reg["p_t3"]        = preco
            reg["mc_t3"]       = mc
            reg["liq_t3"]      = liq
            reg["volume_t3"]   = volume
            reg["holders_t3"]  = holders
            if preco and reg["p_t0"]:
                reg["var_t3_%"] = round((preco - reg["p_t0"]) / reg["p_t0"] * 100, 2)
            reg["veredito_t3"]    = veredito_parcial(reg["mc_t2"], mc, "45min")
            reg["categoria_final"] = categoria_final(reg)
            info["t3_ok"] = True
            log(f"  ✅ [{nome}] FINAL {reg['nome'][:18]} | "
                f"MC: ${mc:,.0f} | Vol: ${volume:,.0f} | "
                f"Txns24h: {holders} | {reg['categoria_final']}")
            del est["pendentes"][mint]

# ── SALVAR ────────────────────────────────────────────────
def salvar(nome):
    est = estado[nome]
    if not est["registros"]:
        return
    pd.DataFrame(est["registros"]).to_csv(est["arquivo_csv"], index=False)
    log(f"💾 [{nome}] Salvo — {len(est['registros'])} registros")

# ── PROCESSAR CARTEIRA ────────────────────────────────────
def processar_carteira(nome, carteira_addr):
    est   = estado[nome]
    txs   = get_ultimas_txs(carteira_addr)
    novas = extrair_novas_compras(txs, carteira_addr, est["tokens_conhecidos"])

    for compra in novas:
        mint = compra["mint"]
        ts   = compra["timestamp"]
        data = datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")

        preco_t0, mc_t0, liq_t0, volume_t0, dex, nome_token, holders_t0 = get_dados_token(mint)

        idx = len(est["registros"])
        est["registros"].append({
            # Identificação
            "data_compra":     data,
            "carteira":        nome,
            "token_mint":      mint,
            "nome":            nome_token,
            "dex":             dex,
            "quantidade":      compra["quantidade"],
            "signature":       compra["signature"],
            # T0
            "p_t0":            preco_t0,
            "mc_t0":           mc_t0,
            "liq_t0":          liq_t0,
            "volume_t0":       volume_t0,
            "holders_t0":      holders_t0,
            # T1
            "p_t1":            None, "mc_t1":     None,
            "liq_t1":          None, "volume_t1": None, "holders_t1": None,
            "var_t1_%":        None, "veredito_t1": None,
            # T2
            "p_t2":            None, "mc_t2":     None,
            "liq_t2":          None, "volume_t2": None, "holders_t2": None,
            "var_t2_%":        None, "veredito_t2": None,
            # T3
            "p_t3":            None, "mc_t3":     None,
            "liq_t3":          None, "volume_t3": None, "holders_t3": None,
            "var_t3_%":        None, "veredito_t3": None,
            # Conclusão
            "categoria_final": "⏳ aguardando",
        })

        est["pendentes"][mint] = {
            "ts_compra": time.time(),
            "idx":       idx,
            "t1_ok":     False,
            "t2_ok":     False,
            "t3_ok":     False,
        }

        log(f"🆕 [{nome}] {nome_token} | DEX: {dex} | "
            f"T0: ${preco_t0} | MC: ${mc_t0:,.0f} | "
            f"Vol: ${volume_t0:,.0f} | Txns24h: {holders_t0}")

    checar_pendentes(nome)

    if time.time() - est["ultimo_save"] >= SALVAR_A_CADA * 60:
        salvar(nome)
        est["ultimo_save"] = time.time()

# ── LOOP PRINCIPAL ────────────────────────────────────────
log("🚀 MONITOR v2 INICIADO — 3 carteiras")
for nome, addr in CARTEIRAS.items():
    log(f"   {nome}: {addr[:20]}...")

ciclo = 0

while True:
    ciclo += 1
    try:
        for nome, addr in CARTEIRAS.items():
            processar_carteira(nome, addr)
            time.sleep(1)

        if ciclo % 20 == 0:
            total = sum(len(estado[n]["registros"]) for n in CARTEIRAS)
            pend  = sum(len(estado[n]["pendentes"]) for n in CARTEIRAS)
            log(f"💓 Ciclo {ciclo} | Registros: {total} | Pendentes: {pend}")

    except Exception as e:
        log(f"⚠️  Erro no ciclo {ciclo}: {e}")

    time.sleep(INTERVALO_VERIFICACAO)
