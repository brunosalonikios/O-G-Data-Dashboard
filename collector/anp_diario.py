"""Produção diária por empresa, do Painel Dinâmico de Produção Diária da ANP (Power BI público).

A ANP calcula a produção diária a partir dos volumes medidos que as operadoras enviam ao Sistema de
Fiscalização da Produção (SFP) em até 3 dias úteis. O painel guarda uns 6 meses. Lemos pela mesma
API pública que o painel usa no navegador: produção por dia e por grupo empresarial do operador.

Cobertura: o SFP pega bem o mar; parte da produção em terra não aparece. Por isso guardamos também
a razão entre a média diária e o arquivo mensal da ANP nos meses em comum (`cobertura`), e o site só
usa o dado diário de uma empresa quando essa razão fica perto de 1.
"""
from __future__ import annotations

import datetime as dt

import requests

PAGINA = ("https://www.gov.br/anp/pt-br/centrais-de-conteudo/paineis-dinamicos-da-anp/"
          "paineis-dinamicos-sobre-exploracao-e-producao-de-petroleo-e-gas/"
          "paineis-dinamicos-de-producao-de-petroleo-e-gas-natural")
CHAVE = "f446236f-7676-436d-b414-c98ef4db8849"     # chave pública do painel diário (link do site da ANP)
CLUSTER = "https://wabi-brazil-south-d-primary-api.analysis.windows.net"
GRUPOS = {"Sistema Petrobras": "Petrobras", "PetroRio": "PRIO", "3R Petroleum": "Brava Energia",
          "Petrorecôncavo S.A.": "PetroReconcavo"}


def _chave_atual(get) -> str:
    """Se a ANP trocar o link do painel diário, pega a chave nova da página."""
    import base64
    import json
    import re
    try:
        h = get(PAGINA).text
        i = h.find("Produção Diária")
        for r in re.findall(r"powerbi\.com/view\?r=([A-Za-z0-9=_-]+)", h[i:] if i > 0 else h):
            return json.loads(base64.b64decode(r + "==="))["k"]
    except Exception:  # noqa: BLE001
        pass
    return CHAVE


def _decode(d) -> list:
    ds = d["results"][0]["result"]["data"]["dsr"]["DS"][0]
    vd = ds.get("ValueDicts", {})
    out, prev, schema = [], None, None
    for row in ds["PH"][0].get("DM0", []):
        if "S" in row:
            schema = row["S"]
            prev = [None] * len(schema)
        rep, nul = row.get("R", 0), row.get("Ø", 0)
        vals = iter(row.get("C", []))
        cur = []
        for i, s in enumerate(schema):
            if rep >> i & 1:
                v = prev[i]
            elif nul >> i & 1:
                v = None
            else:
                v = next(vals)
                if "DN" in s and isinstance(v, int):
                    v = vd[s["DN"]][v]
            cur.append(v)
        out.append(cur)
        prev = cur
    return out


def build(get) -> dict:
    chave = _chave_atual(get)
    s = requests.Session()
    s.headers.update({"X-PowerBI-ResourceKey": chave, "User-Agent": "Mozilla/5.0 (compatible; og-dashboard/1.0)"})
    me = s.get(f"{CLUSTER}/public/reports/{chave}/modelsAndExploration?preferReadOnlySession=true", timeout=120)
    me.raise_for_status()
    mid = me.json()["models"][0]["id"]

    def c(src, p):
        return {"Column": {"Expression": {"SourceRef": {"Source": src}}, "Property": p}, "Name": f"{src}.{p}"}

    def m(p):
        return {"Measure": {"Expression": {"SourceRef": {"Source": "m"}}, "Property": p}, "Name": f"m.{p}"}
    sel = [c("d", "Data"), c("e", "NOM_GRUPO"), m("Petróleo"), m("Gás Mm3")]
    grupo_col = {"Column": {"Expression": {"SourceRef": {"Source": "e"}}, "Property": "NOM_GRUPO"}}
    where = [{"Condition": {"In": {"Expressions": [grupo_col],
                                   "Values": [[{"Literal": {"Value": f"'{g}'"}}] for g in GRUPOS]}}}]
    q = {"Version": 2, "From": [{"Name": "d", "Entity": "Datas", "Type": 0}, {"Name": "m", "Entity": "Medidas", "Type": 0},
                                {"Name": "e", "Entity": "Empresas SIGEP", "Type": 0}],
         "Select": sel, "Where": where}
    body = {"version": "1.0.0", "queries": [{"Query": {"Commands": [{"SemanticQueryDataShapeCommand": {
        "Query": q, "Binding": {"Primary": {"Groupings": [{"Projections": [0, 1, 2, 3]}]},
                                "DataReduction": {"DataVolume": 4, "Primary": {"Window": {"Count": 30000}}}, "Version": 1}}}]}}],
        "cancelQueries": [], "modelId": mid}
    r = s.post(f"{CLUSTER}/public/reports/querydata?synchronous=true", json=body, timeout=300)
    r.raise_for_status()
    dias = {v: {} for v in GRUPOS.values()}
    for data, grupo, oil, gas in _decode(r.json()):
        emp = GRUPOS.get(grupo)
        if not emp or data is None:
            continue
        d = dt.datetime.fromtimestamp(int(data) / 1000, dt.timezone.utc).date().isoformat()
        dias[emp][d] = [round(float(oil or 0)), round(float(gas or 0), 1)]   # bbl/d e mil m³/d
    todos = sorted({d for v in dias.values() for d in v})
    if not todos:
        raise RuntimeError("painel diário sem dados")
    # descarta os últimos dias ainda incompletos (operadoras têm até 3 dias úteis para enviar)
    tot = {d: sum(v.get(d, [0, 0])[0] for v in dias.values()) for d in todos}
    ref = sorted(tot.values())[len(tot) // 2]
    while todos and tot[todos[-1]] < 0.9 * ref:
        todos.pop()
    ultimo = todos[-1]
    dias = {e: {d: x for d, x in sorted(v.items()) if d <= ultimo} for e, v in dias.items()}
    return {"dias": dias, "ultimo_dia": ultimo, "primeiro_dia": todos[0]}
