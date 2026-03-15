"""
Experimento ML — wallet-monitor
Objetivo: medir AUC-ROC de classificação binária (var_pico > 50%)
Rode com: python ml_experiment.py
"""

import sys
import warnings
warnings.filterwarnings('ignore')

# ── Dependências ──────────────────────────────────────────────────────────────
try:
    import psycopg2
    import pandas as pd
    import numpy as np
    from sklearn.linear_model import LogisticRegression
    from sklearn.ensemble import RandomForestClassifier
    from sklearn.metrics import roc_auc_score, classification_report, confusion_matrix
    from sklearn.preprocessing import StandardScaler
    from sklearn.pipeline import Pipeline
except ImportError as e:
    print(f"Instale as dependências: pip install psycopg2-binary pandas scikit-learn")
    print(f"Erro: {e}")
    sys.exit(1)

DB_URL = "postgresql://postgres:OgNvgWkjcpuFxZPHBaASjCKnLNsXKlpI@switchyard.proxy.rlwy.net:47120/railway"

# ── 1. Carregar dados ─────────────────────────────────────────────────────────
print("Conectando ao banco...")
conn = psycopg2.connect(DB_URL)

# Primeiro: ver todas as colunas disponíveis
cols_query = """
    SELECT column_name, data_type
    FROM information_schema.columns
    WHERE table_name = 'sinais'
    ORDER BY ordinal_position
"""
df_cols = pd.read_sql(cols_query, conn)
print("\n=== COLUNAS DISPONÍVEIS EM 'sinais' ===")
print(df_cols.to_string(index=False))

# Carregar tabela completa
df = pd.read_sql("SELECT * FROM sinais", conn)
conn.close()

print(f"\nTotal de registros: {len(df)}")
print(f"Colunas: {list(df.columns)}")

# ── 2. Definir label ──────────────────────────────────────────────────────────
# Precisamos de var_pico para o label — vamos encontrar a coluna certa
pico_cols = [c for c in df.columns if 'pico' in c.lower() or 'peak' in c.lower() or 'max' in c.lower()]
print(f"\nColunas candidatas para label (var_pico): {pico_cols}")

# Tentar nomes comuns
label_col = None
for candidato in ['var_pico', 'variacao_pico', 'peak_var', 'max_var', 'pico']:
    if candidato in df.columns:
        label_col = candidato
        break

if label_col is None and pico_cols:
    label_col = pico_cols[0]
    print(f"Usando '{label_col}' como coluna de label")

if label_col is None:
    print("\n❌ Não encontrei coluna de var_pico. Colunas numéricas disponíveis:")
    print(df.select_dtypes(include='number').columns.tolist())
    print("\nEdite o script e defina label_col manualmente.")
    sys.exit(1)

print(f"\nLabel: '{label_col}' > 50%")
print(f"  Distribuição: {df[label_col].describe()}")

# Remover registros sem label
df = df[df[label_col].notna()].copy()
df['target'] = (df[label_col] > 50).astype(int)
print(f"\nRegistros com label: {len(df)}")
print(f"  Wins (>50%): {df['target'].sum()} ({df['target'].mean()*100:.1f}%)")
print(f"  Losses:      {(~df['target'].astype(bool)).sum()} ({(1-df['target'].mean())*100:.1f}%)")

# ── 3. Selecionar features de T0 ─────────────────────────────────────────────
# Features candidatas — pegamos só as que existem no banco
FEATURES_CANDIDATAS = [
    # Score e componentes principais
    'score', 'score_v7',
    # Market cap
    'mc_t0', 'marketcap', 'market_cap',
    # Holders
    'holders', 'holders_t0',
    # Ratio buy/sell
    'ratio_bs', 'ratio_buy_sell', 'buy_ratio',
    # Volume / momentum
    'net_momentum', 'momentum', 'volume',
    # Idade
    'idade_min', 'age_minutes', 'idade',
    # Bonding curve
    'bc_progress', 'bonding_curve',
    # Multi-carteira
    'is_multi', 'multi_wallet',
    # Top holders
    'top_holder', 'top10', 'top_10_pct',
    # Dev
    'dev_confiavel', 'dev_rugger', 'serial_rugger', 'dev_score',
    # Txns
    'txns', 'transactions',
    # Vol/MC ratio
    'ratio_vol_mc', 'vol_mc',
    # DEX (pumpfun vs outros)
    'is_pumpfun', 'dex_pumpfun',
    # Hora
    'hora', 'hour',
]

features_disponiveis = [f for f in FEATURES_CANDIDATAS if f in df.columns]
print(f"\n=== FEATURES ENCONTRADAS ({len(features_disponiveis)}) ===")
for f in features_disponiveis:
    nulos = df[f].isna().sum()
    print(f"  {f}: {df[f].dtype} | nulos={nulos} ({nulos/len(df)*100:.1f}%)")

if len(features_disponiveis) < 2:
    print("\n❌ Poucas features encontradas. Colunas numéricas do banco:")
    print(df.select_dtypes(include='number').columns.tolist())
    print("\nAdicione as colunas corretas em FEATURES_CANDIDATAS e rode novamente.")
    sys.exit(1)

# ── 4. Preparar dataset ───────────────────────────────────────────────────────
X = df[features_disponiveis].copy()
y = df['target'].copy()

# Tratar booleanos
for col in X.select_dtypes(include='bool').columns:
    X[col] = X[col].astype(int)

# Tratar categóricos simples (dex, etc.)
for col in X.select_dtypes(include='object').columns:
    print(f"  Coluna categórica '{col}': {X[col].unique()[:10]}")
    X[col] = (X[col] == 'pumpfun').astype(int) if 'dex' in col.lower() or 'pumpfun' in col.lower() else X[col].astype('category').cat.codes

# Preencher nulos com mediana
X = X.fillna(X.median(numeric_only=True))

print(f"\nDataset final: {X.shape[0]} registros x {X.shape[1]} features")

# ── 5. Validação temporal ─────────────────────────────────────────────────────
# Ordenar por data se existir
date_col = None
for c in ['created_at', 'timestamp', 'data', 'date', 'dt']:
    if c in df.columns:
        date_col = c
        break

if date_col:
    idx_sorted = df[date_col].argsort().values
    X = X.iloc[idx_sorted].reset_index(drop=True)
    y = y.iloc[idx_sorted].reset_index(drop=True)
    print(f"Ordenado por '{date_col}' (validação temporal)")
    split_label = "temporal"
else:
    print("Sem coluna de data — usando split sequencial (primeiros 80% treino)")
    split_label = "sequencial"

split = int(len(X) * 0.8)
X_train, X_test = X.iloc[:split], X.iloc[split:]
y_train, y_test = y.iloc[:split], y.iloc[split:]

print(f"Treino: {len(X_train)} | Teste: {len(X_test)}")
print(f"  Wins no teste: {y_test.sum()} ({y_test.mean()*100:.1f}%)")

# ── 6. Treinar modelos ────────────────────────────────────────────────────────
print("\n=== TREINANDO MODELOS ===")

modelos = {
    "Regressão Logística": Pipeline([
        ('scaler', StandardScaler()),
        ('model', LogisticRegression(max_iter=1000, class_weight='balanced', random_state=42))
    ]),
    "Random Forest": RandomForestClassifier(
        n_estimators=100, max_depth=4,
        class_weight='balanced', random_state=42
    ),
}

resultados = {}
for nome, modelo in modelos.items():
    modelo.fit(X_train, y_train)
    proba = modelo.predict_proba(X_test)[:, 1]
    pred = modelo.predict(X_test)
    auc = roc_auc_score(y_test, proba)
    resultados[nome] = {'auc': auc, 'proba': proba, 'pred': pred, 'modelo': modelo}
    print(f"\n{nome}: AUC-ROC = {auc:.4f}")
    print(classification_report(y_test, pred, target_names=['Loss', 'Win']))

# ── 7. Feature importance (Random Forest) ────────────────────────────────────
print("\n=== IMPORTÂNCIA DAS FEATURES (Random Forest) ===")
rf = resultados["Random Forest"]['modelo']
importancias = pd.Series(rf.feature_importances_, index=features_disponiveis)
importancias = importancias.sort_values(ascending=False)
for feat, imp in importancias.items():
    barra = '█' * int(imp * 100)
    print(f"  {feat:<25} {imp:.4f}  {barra}")

# ── 8. Análise por threshold de probabilidade ────────────────────────────────
print("\n=== ANÁLISE POR THRESHOLD (Random Forest) ===")
proba_rf = resultados["Random Forest"]['proba']
print(f"{'Threshold':>10} {'N alertas':>10} {'Win rate':>10} {'Cobertura':>10}")
print("-" * 45)
for thresh in [0.40, 0.45, 0.50, 0.55, 0.60, 0.65, 0.70]:
    mask = proba_rf >= thresh
    n = mask.sum()
    if n > 0:
        wr = y_test[mask].mean()
        cob = mask.sum() / len(y_test)
        print(f"  ≥{thresh:.0%}   {n:>8}   {wr:>8.1%}   {cob:>8.1%}")

# ── 9. Resumo final ───────────────────────────────────────────────────────────
print("\n" + "="*50)
print("RESUMO FINAL")
print("="*50)
for nome, r in resultados.items():
    print(f"  {nome}: AUC = {r['auc']:.4f}")
print(f"\nBaseline (aleatório): AUC = 0.5000")
print(f"Win rate geral no teste: {y_test.mean():.1%}")
print(f"\nFeature mais importante: {importancias.index[0]} ({importancias.iloc[0]:.4f})")
print(f"Feature menos importante: {importancias.index[-1]} ({importancias.iloc[-1]:.4f})")
print("\n✅ Experimento concluído.")
