"""Argentina: produção por empresa, gás total e consumo, vendas de combustíveis por bandeira, poços.

Fonte: Ministério da Economia da Argentina (Subsecretaria de Programação Macroeconômica),
séries mensais publicadas em infra.datos.gob.ar a partir dos dados da Secretaria de Energia:
  dataset 365  Producción y ventas de petróleo, gas y derivados por empresa
  dataset 364  Producción y consumo de gas natural
  dataset 366  Pozos de petróleo y metros perforados
O portal da Secretaria de Energia (datos.energia.gob.ar) não responde fora da Argentina,
por isso usamos estas séries espelhadas.
"""
from __future__ import annotations

import calendar
import csv
import io

BASE = "https://infra.datos.gob.ar/catalog/sspm/dataset"
URLS = {
    "empresas": f"{BASE}/365/distribution/365.3/download/actividad-empresas.csv",
    "gas": f"{BASE}/364/distribution/364.3/download/actividad-gas.csv",
    "pozos": f"{BASE}/366/distribution/366.3/download/actividad-pozos.csv",
}
START = "2015-01"
BBL_PER_M3 = 6.28981

OIL = {
    "ypf_sa": "YPF", "pan_american_energy_llc": "Pan American", "pluspetrol_sa": "Pluspetrol",
    "chevron_argentina_srl": "Chevron", "tecpetrol_sa": "Tecpetrol", "total_austral_sa": "TotalEnergies",
    "sinopec_argentina": "Sinopec", "enap_sipetrol_argentina_sa": "ENAP Sipetrol",
    "petrobras_argentina_sa": "Petrobras Argentina",
}
# Na série de gás os rótulos de cinco colunas estão deslocados na fonte. As datas de início e
# fim de cada coluna coincidem com as do petróleo da empresa correta (ex.: a coluna "sinopec"
# existe desde 1999 e vale ~23 mi m³/d em 2026, que é Tecpetrol/Fortín de Piedra).
GAS = {
    "ypf_sa": "YPF", "pan_american_energy_llc": "Pan American", "pluspetrol_sa": "Pluspetrol",
    "sinopec_argentina_exploration": "Tecpetrol", "tecpetrol_sa": "Petrobras Argentina",
    "total_austral_sa": "TotalEnergies", "chevron_argentina_srl": "Sinopec",
    "enap_sipetrol_argentina_sa": "ENAP Sipetrol", "petrobras_argentina_sa": "Chevron",
}
VENDAS = {"ypf": "YPF", "shell": "Raízen (Shell)", "essoaxion": "Axion", "petrobras": "Puma (ex-Petrobras)", "oil": "Oil Combustibles"}


def _rows(content: bytes):
    txt = content.decode("utf-8-sig")
    return list(csv.DictReader(io.StringIO(txt)))


def _f(v):
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return x


def _days(mes: str) -> int:
    y, m = map(int, mes.split("-"))
    return calendar.monthrange(y, m)[1]


def build(get) -> dict:
    emp = _rows(get(URLS["empresas"]))
    gas = _rows(get(URLS["gas"]))
    poz = _rows(get(URLS["pozos"]))

    petroleo, gas_emp, vendas = [], [], []
    for r in emp:
        mes = r["indice_tiempo"][:7]
        if mes < START:
            continue
        d = _days(mes)
        o = {"mes": mes}
        for k, nome in OIL.items():
            v = _f(r.get(f"produccion_petroleo_crudo_{k}"))
            if v:
                o[nome] = round(v / d * BBL_PER_M3 / 1000, 1)          # mil bbl/d (fonte: m³/mês)
        if len(o) > 1:
            petroleo.append(o)
        g = {"mes": mes}
        for k, nome in GAS.items():
            v = _f(r.get(f"produccion_gas_natural_{k}"))
            if v:
                g[nome] = round(v / d / 1000, 2)                        # mi m³/d (fonte: mil m³/mês)
        if len(g) > 1:
            gas_emp.append(g)
        s = {"mes": mes}
        for prod, chave in (("gasoil", "diesel"), ("naftas", "gasolina")):
            for k, nome in VENDAS.items():
                v = _f(r.get(f"ventas_{prod}_{k}"))
                if v:
                    s.setdefault(chave, {})[nome] = round(v / 1000, 1)  # mil m³/mês
        if len(s) > 1:
            vendas.append(s)

    gas_total = []
    for r in gas:
        mes = r["indice_tiempo"][:7]
        if mes < START:
            continue
        d = _days(mes)
        p, c = _f(r.get("produccion_gas_natural")), _f(r.get("total"))
        row = {"mes": mes}
        if p:
            row["producao"] = round(p / d, 1)                           # mi m³/d (fonte: mi m³/mês)
        if c:
            row["consumo"] = round(c / d, 1)
            for k, nome in (("residencial", "Residencial"), ("industria", "Indústria"),
                            ("centrales_electricas", "Usinas elétricas"), ("gnc", "GNV"),
                            ("comercial", "Comercial")):
                v = _f(r.get(k))
                if v is not None:
                    row[nome] = round(v / d, 2)
        if len(row) > 1:
            gas_total.append(row)

    pocos = []
    for r in poz:
        mes = r["indice_tiempo"][:7]
        if mes < START:
            continue
        t = _f(r.get("pozos_terminados_total_general"))
        if t is None:
            continue
        pocos.append({"mes": mes, "pocos": int(t),
                      "explotacao": int(_f(r.get("pozos_terminados_explotacion")) or 0),
                      "exploracao": int(_f(r.get("pozos_terminados_exploracion")) or 0),
                      "metros_mil": round((_f(r.get("metros_perforados_total_general")) or 0) / 1000, 1)})

    # ranking do último mês com dado de petróleo
    def ranking(serie):
        if not serie:
            return None
        last = serie[-1]
        y, m = map(int, last["mes"].split("-"))
        ago = next((r for r in serie if r["mes"] == f"{y - 1:04d}-{m:02d}"), {})
        return {"mes": last["mes"], "itens": sorted(
            [{"empresa": k, "valor": v, "valor_aa": ago.get(k)} for k, v in last.items() if k != "mes"],
            key=lambda t: -t["valor"])}

    return {
        "petroleo_empresas": petroleo, "gas_empresas": gas_emp,
        "ranking_petroleo": ranking(petroleo), "ranking_gas": ranking(gas_emp),
        "vendas_bandeira": vendas, "gas_total": gas_total, "pocos": pocos,
    }
