# Contexto do Projeto — wallet-monitor

## O que é
Sistema de monitoramento de carteiras Solana para identificar oportunidades em memecoins/tokens novos.
Repositório: https://github.com/justafake33/wallet-monitor

## Stack
- Python + Flask + PostgreSQL (Railway) + Telegram Bot
- Deploy: Render (worker service)

## Banco de Dados
```
DATABASE_URL = postgresql://postgres:OgNvgWkjcpuFxZPHBaASjCKnLNsXKlpI@switchyard.proxy.rlwy.net:47120/railway
```

## Carteiras Monitoradas (Solana)
| Alias | Endereço | Tipo |
|-------|----------|------|
| carteira_A | GijFWw4oNyh9ko3FaZforNsi3jk6wDovARpkKahPD4o5 | bot |
| carteira_B | ANfB2knFb7pC7jKadHnSP4xKZ31KJGNLhWRo89LWsFeW | bot |
| carteira_C | 43C9gHfJ7YgqKv5ft3hodFgumydv1nEiNHD1PuANufk5 | humano |
| carteira_D | EvGpkcSBfhp5K9SNP48wVtfNXdKYRBiK3kvMkB66kU3Q | humano |

## Sistema de Score (0-10)
Função `calcular_score()` em `monitor.py`. Score atual é baseado em:
- **Ratio buy/sell**: ≥70% buys → +3, ≥55% → +2, <40% → -2
- **Idade do token**: 25-60min → +2, ≤10min → +1, 10-25min → -2, >120min → -1
- **Market cap**: $30k-$60k → +2, $5k-$30k → +1, >$60k → -2, <$5k → -1
- **Ratio vol/mc**: 1.0-3.0 → +1, <0.8 → -1
- **Net momentum**: ≥20 → +1, <0 → -1
- **Holders**: ≥200 → +1, <80 → -1
- **bc_progress**: <30 → +1, 60-90 → -1
- **txns ≥80**: +1
- **dex == pumpfun**: +1
- **dev confiável**: +2 | **dev rugger**: -3 | **serial_rugger**: score=0 bloqueado
- **Multiplicador horário**: 2-8 UTC → x1.15, 18-20 UTC → x0.85

### Tiers
- ≥7 → 🟢 ALTA CONFIANÇA
- 4-6 → 🟡 MODERADO
- <4 → 🔴 BAIXA CONFIANÇA

## Checkpoints de Performance
- **T0**: momento da compra
- **T1**: 5 minutos depois
- **T2**: 15 minutos depois
- **T3**: 45 minutos depois

## Categorias Finais
- 🏆 VENCEDOR — subiu >200% e manteve >100%
- 🎯 PUMP & DUMP — subiu >50% e colapsou
- 📈 BOM TRADE — pico >50%, final >20%
- ⚠️ ARMADILHA — pico rápido e queda
- 📊 CRESCIMENTO ESTÁVEL
- ➡️ LATERAL
- 💀 MORREU

## Definição de Win/Loss
- **Vitória**: var_pico > +50%
- **Derrota**: var_pico < -50%

## Arquivos Principais
- `monitor.py` — core do sistema, webhooks, score, checkpoints
- `analise.py` — dashboard de performance, correlações, relatórios
- `eda.ipynb` — análise exploratória

## Status Atual

### ✅ Score v7.0 — CONCLUÍDO e em produção
Recalibrado com base em **782 registros reais**. Push feito em 11/03/2026.

**Mudanças aplicadas:**
- `bc_progress`: peso aumentado (+3/+2/+1 / -1/-2) — melhor preditor (r=-0.145)
- `txns>=80`: removido — correlação ~0 com var_pico
- `is_multi`: bônus +1 adicionado — win 43.8% vs 36.4% single
- `ratio_bs`: reduzido de +3 para +2
- `mc_t0`: faixa ampliada 15k-80k (era 30k-60k)
- `idade_min`: corrigida inversão 10-25min (era -2, agora +1)
- Multiplicador de horário: removido

**Dados da análise (11/03/2026):**
- 🟢 ALTA CONFIANÇA: n=167, win=42.5%, med=30.9%
- 🟡 MODERADO: n=243, win=38.7%, med=29.3%
- 🔴 BAIXA CONFIANÇA: n=372, win=32.5%, med=21.0%
- Multi-carteira: win=43.8% | Single: win=36.4%

### Próximos passos pendentes
- Aguardar novos dados com score v7.0 para validar melhoria
- Opcional: recalcular scores históricos dos 782 registros com nova fórmula (usuário decidirá depois)
- ⏳ LEMBRETE ML — Peso temporal (decay) na recalibração v8.0: quando acumular ~200-300 registros novos (pós v7.0), recalibrar usando `peso = exp(-λ * dias_atrás)` para dar mais peso aos dados recentes. Os 782 históricos são in-sample (usados pra construir v7.0) e o mercado de memecoin sofre concept drift.

## Dashboard HTML
O usuário tem um dashboard HTML local que lê ao vivo de:
`https://[render-url]/dados?key=neide12`
Atualiza a cada 15s automaticamente. Não precisa de nova versão.

## APIs Externas
- **DexScreener**: dados de tokens
- **Helius RPC**: blockchain Solana (API key: 4f586430-90ef-4c8f-9800-b98bfe5f1151)
- **CoinGecko**: preço do SOL
- **Telegram**: alertas (chat: -5284184650)
