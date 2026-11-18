# Petróleo e Gás no Brasil — dashboard automático

Painel do mercado brasileiro de petróleo e gás que se atualiza sozinho a cada hora
(GitHub Actions + GitHub Pages). Não há nenhuma entrada manual.

## Como funciona

- `collector/collect.py` baixa as fontes públicas, calcula paridade de importação,
  defasagem e crack spreads e grava `site/data/dashboard.json`.
- `.github/workflows/update.yml` roda o coletor de hora em hora, salva no repositório
  só quando os dados mudam e publica `site/` no GitHub Pages.
- `data/cache/` guarda o histórico já baixado (produção por campo, Comex Stat) para
  que cada rodada só busque o que é novo. `data/ref/campo_operador.json` é o mapa
  campo → operador extraído uma vez do arquivo da ANP de dez/2023.
- Se uma fonte falhar, o bloco anterior é mantido e a falha aparece no topo da página.

## Fontes

| Bloco | Fonte |
|---|---|
| Brent, WTI, diesel e gasolina USGC | EIA (planilhas históricas; FRED como reserva) |
| Câmbio | Banco Central, SGS série 1 (PTAX venda) |
| Produção oficial | ANP, produção por estado e localização |
| Pré-sal, campos, operadores | ANP, produção por poço/zona (mensal) |
| Vendas | ANP, vendas de derivados e etanol |
| Preço na bomba | ANP, levantamento semanal de preços |
| PPI oficial (só calibração) | ANP, preços de paridade de importação |
| Preço Petrobras sem tributos | Petrobras, tabelas de preços às distribuidoras (Cubatão-SP, EXA) |
| Comércio exterior | Comex Stat (MDIC), NCM 2709 e 2710 |
| Market share e volume por distribuidora, canais, fornecedores primários | ANP, Painel Dinâmico do Mercado de Combustíveis Líquidos (`mdpg/liquidos.zip`) |
| Argentina: produção por empresa, vendas por bandeira, gás, poços | Ministério da Economia da Argentina, séries 365, 364 e 366 em infra.datos.gob.ar |

## Metodologia

- PPI (R$/L) = (P_USGC [US$/gal] ÷ 3,785 + k) × PTAX, com k (frete, seguro, perdas,
  custos portuários, AFRMM, armazenagem) recalibrado toda semana pela mediana das
  últimas 4 semanas da diferença para a PPI da ANP em Santos.
- Defasagem = preço Petrobras ÷ PPI − 1 (cálculo próprio, não usa a ABICOM).
- Crack USGC = produto × 42 − Brent; crack Brasil implícito = preço Petrobras em US$/bbl − Brent.

Agendamento: o cron do GitHub pula execuções; `.github/workflows/relogio.yml` fica rodando
e dispara `update.yml` a cada hora (o cron fica como reserva).

Rodar localmente: `pip install -r requirements.txt && python collector/collect.py`,
depois `python -m http.server -d site`.
