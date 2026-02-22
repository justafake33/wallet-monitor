import requests
import pandas as pd
import time
import os
from datetime import datetime, timezone

HELIUS_API_KEY = "6ccebda4-8501-4224-a238-03d909a0d893"

CARTEIRAS = {
    "carteira_A": "GijFWw4oNyh9ko3FaZforNsi3jk6wDovARpkKahPD4o5",
    "carteira_B": "ANfB2knFb7pC7jKadHnSP4xKZ31KJGNLhWRo89LWsFeW",
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

# Estado por carteira
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

def get_ultimas_txs(carteira):
    url = f"https://api.helius.xyz/v0/addresses/{carteira}/transactions?api-key={HELIUS_API_KEY}"
    try:
        r = requests.get(url, params={"limit": 10}, timeout=15)
        return r.json() if r.status_code == 200 else []
    except Exception as e:
        log(f"⚠️  Erro ao buscar txs: {e}")
        return []

def get_preco_atual(mint):
    try:
        r = requests.get(
            f"https://api.dexscreener.com/latest/dex/tokens/{mint}",
            timeout=8
        )
        pairs = r.json().get("pairs") or []
        if pairs:
            par = sorted(
                pairs,
                key=lambda x: x.get("liquidity", {}).get("usd", 0) or 0,
                reverse=True
            )[0]
            preco = par.get("priceUsd")
            return (
                float(preco) if preco else None,
                par.get("marketCap") or 0,
                par.get("liquidity", {}).get("usd") or 0,
                par.get("dexId", "?"),
                par.get("baseToken", {}).get("name", "?"),
            )
    except:
        pass
    return None, 0, 0, "?", "?"

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

def checar_pendentes(nome):
    est   = estado[nome]
    agora = time.time()

    for mint, info in list(est["pendentes"].items()):
        ts  = info["ts_compra"]
        idx = info["idx"]
        reg = est["registros"][idx]

        # T1 — 5 minutos
        if not info["t1_ok"] and agora >= ts + 5 * 60:
            preco, mc, _, _, _ = get_preco_atual(mint)
            reg["p_t1"]  = preco
            reg["mc_t1"] = mc
            if preco and reg["p_t0"]:
                reg["var_t1_%"] = round((preco - reg["p_t0"]) / reg["p_t0"] * 100, 2)
            info["t1_ok"] = True
            log(f"  ⏱️  [{nome}] T1 {reg['nome'][:18]} → ${preco} | {reg.get('var_t1_%','N/A')}%")

        # T2 — 15 minutos
        if not info["t2_ok"] and agora >= ts + 15 * 60:
            preco, mc, _, _, _ = get_preco_atual(mint)
            reg["p_t2"]  = preco
            reg["mc_t2"] = mc
            if preco and reg["p_t0"]:
                reg["var_t2_%"] = round((preco - reg["p_t0"]) / reg["p_t0"] * 100, 2)
            info["t2_ok"] = True
            log(f"  ⏱️  [{nome}] T2 {reg['nome'][:18]} → ${preco} | {reg.get('var_t2_%','N/A')}%")

        # T3 — 45 minutos
        if not info["t3_ok"] and agora >= ts + 45 * 60:
            preco, mc, _, _, _ = get_preco_atual(mint)
            reg["p_t3"]  = preco
            reg["mc_t3"] = mc
            if preco and reg["p_t0"]:
                reg["var_t3_%"] = round((preco - reg["p_t0"]) / reg["p_t0"] * 100, 2)
            info["t3_ok"] = True

            v3 = reg.get("var_t3_%")
            if v3 is None:   sinal = "❓ sem preço"
            elif v3 > 100:   sinal = "🚀 EXPLODIU"
            elif v3 > 20:    sinal = "📈 SUBIU FORTE"
            elif v3 > 0:     sinal = "📊 SUBIU POUCO"
            elif v3 > -20:   sinal = "➡️ ESTÁVEL"
            else:            sinal = "📉 CAIU"
            reg["sinal"] = sinal

            log(f"  ✅ [{nome}] T3 {reg['nome'][:18]} → ${preco} | {v3}% | {sinal}")
            del est["pendentes"][mint]

def salvar(nome):
    est = estado[nome]
    if not est["registros"]:
        return
    pd.DataFrame(est["registros"]).to_csv(est["arquivo_csv"], index=False)
    log(f"💾 [{nome}] Salvo — {len(est['registros'])} registros em {est['arquivo_csv']}")

def processar_carteira(nome, carteira_addr):
    est   = estado[nome]
    txs   = get_ultimas_txs(carteira_addr)
    novas = extrair_novas_compras(txs, carteira_addr, est["tokens_conhecidos"])

    for compra in novas:
        mint = compra["mint"]
        ts   = compra["timestamp"]
        data = datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")

        preco_t0, mc_t0, liq_t0, dex, nome_token = get_preco_atual(mint)

        idx = len(est["registros"])
        est["registros"].append({
            "data_compra": data,
            "token_mint":  mint,
            "nome":        nome_token,
            "dex":         dex,
            "quantidade":  compra["quantidade"],
            "p_t0":        preco_t0,
            "mc_t0":       mc_t0,
            "liq_t0":      liq_t0,
            "p_t1":        None, "p_t2": None, "p_t3": None,
            "mc_t1":       None, "mc_t2": None, "mc_t3": None,
            "var_t1_%":    None, "var_t2_%": None, "var_t3_%": None,
            "sinal":       "⏳ aguardando",
            "signature":   compra["signature"],
        })

        est["pendentes"][mint] = {
            "ts_compra": time.time(),
            "idx":       idx,
            "t1_ok":     False,
            "t2_ok":     False,
            "t3_ok":     False,
        }

        log(f"🆕 [{nome}] {nome_token} | DEX: {dex} | T0: ${preco_t0} | MC: ${mc_t0:,.0f}")

    checar_pendentes(nome)

    # Salva automaticamente a cada X minutos
    if time.time() - est["ultimo_save"] >= SALVAR_A_CADA * 60:
        salvar(nome)
        est["ultimo_save"] = time.time()

# ── LOOP PRINCIPAL ────────────────────────────────────────
log("🚀 MONITOR INICIADO — 2 carteiras")
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
            log(f"💓 Ciclo {ciclo} | Total registros: {total} | Pendentes: {pend}")

    except Exception as e:
        log(f"⚠️  Erro no ciclo {ciclo}: {e}")

    time.sleep(INTERVALO_VERIFICACAO)
