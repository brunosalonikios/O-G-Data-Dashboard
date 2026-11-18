"""Downstream Brasil: vendas por distribuidora, canais e fornecedores primários.

Fonte: ANP, "Painel Dinâmico do Mercado Brasileiro de Combustíveis Líquidos",
arquivo aberto mdpg/liquidos.zip (vendas das distribuidoras por agente, produto,
UF e mercado destinatário; entregas de produtores/importadores às distribuidoras).
"""
from __future__ import annotations

import csv
import io
import re
import unicodedata
import zipfile
from collections import defaultdict

URL = "https://www.gov.br/anp/pt-br/centrais-de-conteudo/dados-abertos/arquivos/mdpg/liquidos.zip"
START = "2018-01"
PRODUTOS_VENDA = {"Diesel B": "diesel", "Gasolina C": "gasolina", "Etanol Hidratado": "etanol"}
PRODUTOS_FORN = {"Diesel A": "diesel", "Gasolina A": "gasolina"}
CANAIS = {
    "POSTO DE COMBUSTÍVEIS - BANDEIRADO": "Posto bandeirado",
    "POSTO DE COMBUSTÍVEIS - BANDEIRA BRANCA": "Posto bandeira branca",
    "TRR": "TRR", "TRRNI": "TRR", "CONSUMIDOR FINAL": "Consumidor final",
}


def _n(s: str) -> str:
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode()
    return re.sub(r"\s+", " ", s).strip().upper()


def _num(s: str) -> float:
    s = (s or "").strip().strip('"')
    if not s:
        return 0.0
    if "," in s:
        s = s.replace(".", "").replace(",", ".")
    try:
        return float(s)
    except ValueError:
        return 0.0


def distribuidora(nome: str) -> str:
    n = _n(nome)
    for k, v in (("VIBRA", "Vibra"), ("PETROBRAS DISTRIBUIDORA", "Vibra"), ("IPIRANGA", "Ipiranga"),
                 ("RAIZEN", "Raízen"), ("ATEM", "Atem"), ("ALE COMBUSTIVEIS", "ALE"),
                 ("SABBA", "Sabbá"), ("CIAPETRO", "Ciapetro"), ("PETROBAHIA", "Petrobahia"),
                 ("ROYAL FIC", "Royal FIC"), ("LARCO", "Larco"), ("FEDERAL ENERGIA", "Federal Energia"),
                 ("CENTROESTE", "Centroeste"), ("TOTALENERGIES", "TotalEnergies"), ("TAURUS", "Taurus"),
                 ("SIMARELLI", "Simarelli"), ("RODOIL", "Rodoil"), ("SP INDUSTRIA", "SP"), ("FIC ", "FIC"),
                 ("POTENCIAL", "Potencial"), ("MAXSUL", "Maxsul"), ("76 OIL", "76 Oil"), ("D'MAIS", "D'Mais"),
                 ("GRAN PETRO", "Gran Petro"), ("STANG", "Stang"), ("SMALL", "Small"), ("UNI COMBUSTIVEIS", "Uni")):
        if k in n:
            return v
    n = re.sub(r"\b(S/?A|S\.A\.?|LTDA\.?|EIRELI|ME|EPP)\b", "", n)
    n = re.sub(r"\b(DISTRIBUIDORA|DISTRIBUICAO|COMERCIO|COMERCIAL|DE|DO|DA|DOS|DERIVADOS|PETROLEO|COMBUSTIVEIS|PRODUTOS|E)\b", " ", n)
    n = re.sub(r"[^A-Z0-9' ]", " ", n)
    n = re.sub(r"\s+", " ", n).strip()
    return n.title()[:24] or nome[:24]


def fornecedor(nome: str) -> str:
    n = _n(nome)
    if "PETROLEO BRASILEIRO" in n or n.startswith("PETROBRAS"):
        return "Petrobras"
    if "MATARIPE" in n or "ACELEN" in n:
        return "Acelen (Mataripe)"
    if "REFINARIA DE MANAUS" in n or "REAM" in n or "ATEM" in n and "REFIN" in n:
        return "Ream (Manaus)"
    if "TRADING" in n or "IMPORTA" in n or "EXPORTA" in n:
        return "Importadoras"
    return "Outros produtores"


def _iter_csv(z: zipfile.ZipFile, name: str):
    raw = z.read(name)
    txt = raw.decode("utf-8-sig") if raw[:3] == b"\xef\xbb\xbf" else raw.decode("latin1")
    rd = csv.reader(io.StringIO(txt), delimiter=";")
    hdr = [h.strip().strip('"') for h in next(rd)]
    for row in rd:
        yield dict(zip(hdr, [c.strip().strip('"') for c in row]))


def build(content: bytes) -> dict:
    z = zipfile.ZipFile(io.BytesIO(content))
    names = z.namelist()
    vol = defaultdict(lambda: defaultdict(lambda: defaultdict(float)))     # prod -> mes -> dist -> mil m³
    can = defaultdict(lambda: defaultdict(lambda: defaultdict(float)))     # prod -> mes -> canal
    uf = defaultdict(lambda: defaultdict(lambda: defaultdict(float)))      # prod -> mes -> UF destino
    for name in names:
        if not name.startswith("Liquidos_Vendas"):
            continue
        for r in _iter_csv(z, name):
            p = PRODUTOS_VENDA.get(r.get("Nome do Produto", ""))
            if not p:
                continue
            mes = f"{int(r['Ano']):04d}-{int(r.get('Mês') or r.get('Mes')):02d}"
            if mes < START:
                continue
            q = _num(r.get("Quantidade de Produto (mil m³)", ""))
            vol[p][mes][distribuidora(r.get("Agente Regulado", ""))] += q
            can[p][mes][CANAIS.get(r.get("Mercado Destinatário", ""), "Outros")] += q
            uf[p][mes][r.get("UF Destino", "")] += q
    sup = defaultdict(lambda: defaultdict(lambda: defaultdict(float)))
    for name in names:
        if not name.startswith("Liquidos_Entregas"):
            continue
        hist = "Historico" in name
        if not (hist or "Fornecedor" in name):
            continue
        for r in _iter_csv(z, name):
            p = PRODUTOS_FORN.get(r.get("Nome do Produto", ""))
            if not p:
                continue
            mes = f"{int(r['Ano']):04d}-{int(r.get('Mês') or r.get('Mes')):02d}"
            if mes < START:
                continue
            who = r.get("Fornecedor Destino") if hist else r.get("Fornecedor")
            sup[p][mes][fornecedor(who or "")] += _num(r.get("Quantidade de Produto (mil m³)", ""))

    out = {"fonte_meses": {}, "produtos": {}}
    for p in PRODUTOS_VENDA.values():
        months = sorted(vol[p])
        if not months:
            continue
        # descarta último mês se claramente incompleto
        tot = {m: sum(vol[p][m].values()) for m in months}
        while len(months) > 2 and tot[months[-1]] < 0.7 * tot[months[-2]]:
            months.pop()
        last = months[-1]
        y, m = map(int, last.split("-"))
        yago = f"{y - 1:04d}-{m:02d}"
        l12 = months[-12:]
        p12 = [f"{int(k[:4]) - 1:04d}{k[4:]}" for k in l12]
        acc12 = defaultdict(float)
        accp12 = defaultdict(float)
        for k in l12:
            for d, v in vol[p][k].items():
                acc12[d] += v
        for k in p12:
            for d, v in vol[p].get(k, {}).items():
                accp12[d] += v
        t12, tp12 = sum(acc12.values()), sum(accp12.values())
        top = [d for d, _ in sorted(acc12.items(), key=lambda t: -t[1])[:12]]
        big3 = ["Vibra", "Ipiranga", "Raízen"]
        ranking = []
        for d in top:
            cur = vol[p][last].get(d, 0.0)
            old = vol[p].get(yago, {}).get(d)
            ranking.append({
                "empresa": d,
                "mes_mil_m3": round(cur, 1),
                "mes_share": round(cur / tot[last], 4) if tot[last] else None,
                "mes_share_aa": round(old / tot[yago], 4) if old and tot.get(yago) else None,
                "u12m_mil_m3": round(acc12[d], 0),
                "u12m_share": round(acc12[d] / t12, 4) if t12 else None,
                "u12m_var_volume": round(acc12[d] / accp12[d] - 1, 4) if accp12.get(d) else None,
            })
        serie = []
        for k in months:
            t = tot[k] or 1
            row = {"mes": k, "total_mil_m3": round(tot[k], 1)}
            for d in big3:
                row[d] = round(vol[p][k].get(d, 0.0) / t, 4)
            row["Demais"] = round(1 - sum(row[d] for d in big3), 4)
            serie.append(row)
        canais = []
        for k in months:
            t = sum(can[p][k].values()) or 1
            canais.append({"mes": k, **{c: round(v / t, 4) for c, v in can[p][k].items()}})
        ufs_last = sorted(uf[p][last].items(), key=lambda t: -t[1])
        out["produtos"][p] = {
            "mes": last, "mes_comparacao": yago if yago in tot else None,
            "total_mes_mil_m3": round(tot[last], 1),
            "total_u12m_mil_m3": round(t12, 0),
            "var_u12m": round(t12 / tp12 - 1, 4) if tp12 else None,
            "n_distribuidoras_mes": sum(1 for v in vol[p][last].values() if v > 0),
            "ranking": ranking, "serie_big3": serie, "canais": canais,
            "uf_mes": [{"uf": u, "mil_m3": round(v, 1)} for u, v in ufs_last[:27]],
        }
        out["fonte_meses"][p] = [months[0], last]
    forn = {}
    for p in PRODUTOS_FORN.values():
        months = sorted(sup[p])
        if not months:
            continue
        rows = []
        for k in months:
            t = sum(sup[p][k].values()) or 1
            rows.append({"mes": k, "total_mil_m3": round(t, 1), **{g: round(v / t, 4) for g, v in sup[p][k].items()}})
        forn[p] = rows
    out["fornecedores"] = forn
    return out
