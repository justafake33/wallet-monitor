import psycopg2

conn = psycopg2.connect('postgresql://postgres:OgNvgWkjcpuFxZPHBaASjCKnLNsXKlpI@switchyard.proxy.rlwy.net:47120/railway')
cur = conn.cursor()

cur.execute('''
SELECT
    COUNT(*) as n,
    ROUND(MIN(liq_t0/mc_t0)::numeric, 3) as min,
    ROUND(PERCENTILE_CONT(0.10) WITHIN GROUP (ORDER BY liq_t0/mc_t0)::numeric, 3) as p10,
    ROUND(PERCENTILE_CONT(0.25) WITHIN GROUP (ORDER BY liq_t0/mc_t0)::numeric, 3) as p25,
    ROUND(PERCENTILE_CONT(0.50) WITHIN GROUP (ORDER BY liq_t0/mc_t0)::numeric, 3) as p50,
    ROUND(PERCENTILE_CONT(0.75) WITHIN GROUP (ORDER BY liq_t0/mc_t0)::numeric, 3) as p75,
    ROUND(PERCENTILE_CONT(0.90) WITHIN GROUP (ORDER BY liq_t0/mc_t0)::numeric, 3) as p90,
    ROUND(MAX(liq_t0/mc_t0)::numeric, 3) as max
FROM tokens
WHERE liq_t0 > 0 AND mc_t0 > 0
''')
row = cur.fetchone()
print(f'n={row[0]}')
print(f'min={row[1]}  p10={row[2]}  p25={row[3]}  p50={row[4]}  p75={row[5]}  p90={row[6]}  max={row[7]}')

cur.execute('''
SELECT
    CASE
        WHEN liq_t0/mc_t0 < 0.10 THEN '1_<0.10'
        WHEN liq_t0/mc_t0 < 0.20 THEN '2_0.10-0.20'
        WHEN liq_t0/mc_t0 < 0.30 THEN '3_0.20-0.30'
        WHEN liq_t0/mc_t0 < 0.40 THEN '4_0.30-0.40'
        WHEN liq_t0/mc_t0 < 0.50 THEN '5_0.40-0.50'
        WHEN liq_t0/mc_t0 < 0.70 THEN '6_0.50-0.70'
        WHEN liq_t0/mc_t0 < 1.00 THEN '7_0.70-1.00'
        ELSE                          '8_>=1.00'
    END as faixa,
    COUNT(*) as n,
    ROUND(AVG(CASE WHEN var_pico > 50 THEN 1.0 ELSE 0.0 END)*100, 1) as win_pct,
    ROUND(AVG(var_pico)::numeric, 1) as med_var_pico
FROM tokens
WHERE liq_t0 > 0 AND mc_t0 > 0 AND var_pico IS NOT NULL
GROUP BY 1
ORDER BY 1
''')
print()
print(f'{"faixa liq/mc":<14} | {"n":>4} | {"win%":>5} | med_var_pico')
print('-' * 45)
for r in cur.fetchall():
    label = r[0][2:]  # remove prefixo de ordenação
    print(f'{label:<14} | {r[1]:>4} | {r[2]:>4}%  | {r[3]}%')

conn.close()
