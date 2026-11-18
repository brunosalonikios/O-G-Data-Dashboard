"""EBITDA ajustado recorrente por m³ das distribuidoras, trimestral, a partir do material de RI.

Vibra (Rede de Postos): "Planilhas por Segmento" listadas pela API pública do provedor de RI (MZ).
  EBITDA ajustado do segmento menos recuperações tributárias e vendas de imóveis (os itens que a Vibra
  trata como não recorrentes), dividido pelo volume do segmento. Bate com o release (3T26 = 412).
Raízen (Distribuição Brasil): o EBITDA ajustado da Raízen já exclui os itens não recorrentes.
Ipiranga: linha "Margem EBITDA Ajustado recorrente" dos releases da Ultrapar; releases antigos, sem
  essa linha, usam EBITDA ajustado ÷ volume.
"""
from __future__ import annotations

import datetime as dt
import io
import json
import re

MZ_META = "https://apicatalog.mziq.com/filemanager/company/{cid}/filter/categories/year/meta"
VIBRA_CID, VIBRA_CAT = "d243bdaa-0468-4f64-8c09-ba0bcee9789b", "central-planilhas-segmento"
TRI = re.compile(r"^([1-4])T(\d{2})$")


def _latest(post, cid, cat):
    metas = []
    y = dt.date.today().year
    for ano in (y, y - 1):
        body = {"year": str(ano), "categories": [cat], "language": "pt_BR", "published": True}
        r = json.loads(post(MZ_META.format(cid=cid), body).content)
        metas += r.get("data", {}).get("document_metas", [])
    if not metas:
        raise RuntimeError("nenhum arquivo listado")
    return max(metas, key=lambda m: m["file_date"])


def _serie(content: bytes, padrao: str, aba: str) -> list[dict]:
    import openpyxl
    wb = openpyxl.load_workbook(io.BytesIO(content), read_only=True, data_only=True)
    rx = re.compile(padrao, re.I)
    for ws in [w for w in wb.worksheets if re.search(aba, w.title, re.I)]:
        header, achado = None, None
        for r in ws.iter_rows(values_only=True):
            if header is None and sum(1 for c in r if isinstance(c, str) and TRI.match(c.strip())) >= 4:
                header = r
                continue
            lab = next((c for c in r[:4] if isinstance(c, str)), None)
            if header is not None and lab and rx.search(lab):
                out = []
                for h, v in zip(header, r):
                    m = TRI.match(h.strip()) if isinstance(h, str) else None
                    if m and isinstance(v, (int, float)):
                        out.append({"tri": f"20{m.group(2)}-T{m.group(1)}", "rotulo": h.strip(), "valor": round(v, 1)})
                if out:
                    achado = out          # fica com a última ocorrência (a consolidada, no fim da aba)
        if achado:
            return achado
    raise RuntimeError("linha de EBITDA/m³ não encontrada")


def _bloco(ws, secao: str, linhas: dict) -> dict:
    """Linhas de uma seção (ex.: "REDE DE POSTOS") de uma aba trimestral: {chave: {"3T26": valor}}."""
    header, sec, out = None, None, {}
    for r in ws.iter_rows(values_only=True):
        if header is None:
            if sum(1 for c in r if isinstance(c, str) and TRI.match(c.strip())) >= 4:
                header = r
            continue
        lab = next((c for c in r[:4] if isinstance(c, str)), None)
        if lab and not any(isinstance(c, (int, float)) for c in r) and not any(isinstance(c, str) and c.strip() == "-" for c in r):
            if not lab.startswith("("):
                sec = lab
            continue
        if not (lab and sec and re.search(secao, sec, re.I)):
            continue
        for k, rx in linhas.items():
            if k not in out and re.search(rx, lab, re.I):
                out[k] = {h.strip(): (v if isinstance(v, (int, float)) else 0.0)
                          for h, v in zip(header, r) if isinstance(h, str) and TRI.match(h.strip())}
    return out


def _vibra(get, post) -> dict:
    import openpyxl
    meta = _latest(post, VIBRA_CID, VIBRA_CAT)
    wb = openpyxl.load_workbook(io.BytesIO(get(meta["file_url"]).content), read_only=True, data_only=True)
    aba = lambda rx: next(w for w in wb.worksheets if re.search(rx, w.title, re.I))
    vol = _bloco(aba(r"volume"), r"^rede de postos", {"v": r"^\(mil m"})["v"]
    eb = _bloco(aba(r"resultado por segmento"), r"^rede de postos", {"e": r"^ebitda ajustado"})["e"]
    aj = _bloco(aba(r"reconcilia.*despesas"), r"^rede de postos",
                {"extra": r"recupera..o tribut.ria extra", "outras": r"outras recupera", "imoveis": r"venda de im"})
    if len(aj) < 2:
        raise RuntimeError("Vibra: ajustes não recorrentes não encontrados")
    serie = []
    for rot, v in vol.items():
        if rot in eb and v:
            m = TRI.match(rot)
            rec = eb[rot] + sum(a.get(rot, 0) for a in aj.values())
            serie.append({"tri": f"20{m.group(2)}-T{m.group(1)}", "rotulo": rot, "valor": round(rec * 1000 / v, 1)})
    serie.sort(key=lambda r: r["tri"])
    return {"arquivo": meta["file_title"] + " (Rede de Postos)", "data_arquivo": meta["file_date"][:10], "serie": serie}


RAIZEN_SITE = "e24e1732-fe1c-4eaf-9a28-ed09ccc472b1"
RAIZEN_API = "https://api.solutions-ir.com/v2/files/{site}/period"


def _raizen(get, cache: dict | None = None) -> dict:
    """Raízen: "Planilha por Segmento" de cada ano-safra (abr-mar). Cada arquivo traz poucos
    trimestres, então juntamos todos os arquivos listados. 1T de 26/27 = abr-jun/2026."""
    y = dt.date.today().year
    url = RAIZEN_API.format(site=RAIZEN_SITE) + f"?language=pt&categories=planilhas_por_segmento&startDate=2018-01-01&endDate={y + 2}-12-31"
    files = json.loads(get(url).content)["categories"].get("planilhas_por_segmento", [])
    if not files:
        raise RuntimeError("Raízen: nenhuma planilha listada")
    files.sort(key=lambda f: f["refDate"])
    vals = {}
    for f in files:                       # mais novo por último: revisões prevalecem
        for r in _serie(get(f["url"]).content, r"margem ebitda ajustad.*r\$/m", r"distribui.*br|combust.*br"):
            q, yy = int(r["rotulo"][0]), 2000 + int(r["rotulo"][2:])
            cal_y, cal_q = (yy - 1, q + 1) if q <= 3 else (yy, 1)
            vals[f"{cal_y}-T{cal_q}"] = {"tri": f"{cal_y}-T{cal_q}", "rotulo": f"{q}T {yy - 2001:02d}'{yy - 2000:02d}", "valor": r["valor"], "fonte": "planilha"}
    if cache is not None:                 # releases (CVM) prevalecem sobre a planilha
        for k, v in _raizen_releases(get, cache).items():
            cy, cq = int(k[:4]), int(k[-1])
            fy = cy + 1 if cq > 1 else cy  # ano-safra termina em março
            fq = cq - 1 if cq > 1 else 4
            vals[k] = {"tri": k, "rotulo": f"{fq}T {fy - 2001:02d}'{fy - 2000:02d}", "valor": v["valor"],
                       "fonte": "release, EBITDA normalizado" if v["tipo"] == "norm" else "release"}
    # Convenção de mercado (pedido da Andrea): o trimestre nT do ano-safra AA'BB aparece como nT de 20AA,
    # ex.: 2T 26'27 (jul-set/26) no 2T26. Ou seja, um trimestre antes do calendário civil.
    serie = []
    for k in sorted(vals):
        r = dict(vals[k])
        r["tri"] = _tri_menos(k, 1)
        serie.append(r)
    return {"arquivo": "Planilha por Segmento " + files[-1]["refDate"][:4], "data_arquivo": files[-1].get("createdAt", "")[:10],
            "serie": serie}


# ---------------- Ipiranga: releases de resultados da Ultrapar entregues à CVM (IPE)
CVM_IPE = "https://dados.cvm.gov.br/dados/CIA_ABERTA/DOC/IPE/DADOS/ipe_cia_aberta_{ano}.zip"
ULTRAPAR_CVM = "18465"
NUM = r"\(?-?[\d.]+\)?"


def _n(tok: str) -> float:
    neg = tok.startswith("(")
    v = float(tok.strip("()").replace(".", ""))
    return -v if neg else v


def _ipiranga_release(pdf: bytes):
    """Tabela da Ipiranga no release, três primeiras colunas (trimestre, mesmo trimestre do ano anterior,
    trimestre anterior): margem EBITDA ajustado recorrente; sem ela, EBITDA ajustado ÷ volume."""
    import pdfplumber
    with pdfplumber.open(io.BytesIO(pdf)) as p:
        for pg in p.pages[:15]:
            t = pg.extract_text() or ""
            if not re.search(r"ipiranga", t, re.I) or "Volume total (mil m" not in t:
                continue
            vol = re.search(rf"Volume total \(mil m.\)\s+({NUM})\s+({NUM})\s+({NUM})", t)
            eb = None
            for line in t.split("\n"):
                if re.match(r"EBITDA( Ajustado)?( \(R\$ milh.es\))?\s+\(?-?[\d.]+", line) and not re.search(r"recorrente|IFRS|P.s-ajuste", line, re.I):
                    eb = re.findall(NUM, line.split(")", 1)[-1] if "milh" in line else line[line.index(" ", 7):])[:3]
                    break
            per = re.search(r"\b([1-4])T(\d{2})\s+([1-4])T(\d{2})\s+([1-4])T(\d{2})", t)
            if not per:
                continue
            tri = [f"20{per.group(i + 1)}-T{per.group(i)}" for i in (1, 3, 5)]
            rec = re.search(rf"Margem EBITDA Ajustado recorrente \(R\$/m.\)\s+({NUM})\s+({NUM})\s+({NUM})", t)
            if rec:
                return {q: float(_n(x)) for q, x in zip(tri, rec.groups())}, True
            if vol and eb and len(eb) == 3:
                return {q: round(_n(e) * 1000 / _n(v), 1) for q, e, v in zip(tri, eb, vol.groups()) if _n(v) > 0}, False
    return None


def _ipe(get, codigo: str, aceita=lambda r: True) -> dict:
    """Releases (Tipo Press-release) de uma companhia nos dados abertos IPE da CVM: {Data_Referencia: linha}."""
    import csv
    import zipfile
    rel = {}
    for ano in range(2019, dt.date.today().year + 1):
        try:
            z = zipfile.ZipFile(io.BytesIO(get(CVM_IPE.format(ano=ano)).content))
        except Exception as e:  # noqa: BLE001
            print(f"IPE {ano} falhou: {e}")
            continue
        for name in z.namelist():
            for r in csv.DictReader(io.StringIO(z.read(name).decode("latin1")), delimiter=";"):
                if r["Codigo_CVM"] == codigo and r["Tipo"] == "Press-release" and aceita(r):
                    d = r["Data_Referencia"]
                    if d not in rel or int(r["Versao"]) > int(rel[d]["Versao"]):
                        rel[d] = r
    return rel


RAIZEN_CVM = "25917"


def _tri_menos(q: str, n: int) -> str:
    y, t = int(q[:4]), int(q[-1])
    k = y * 4 + t - 1 - n
    return f"{k // 4}-T{k % 4 + 1}"


def _raizen_release(pdf: bytes):
    """Tabela de Distribuição (Brasil) do relatório de resultados: margem EBITDA ajustada (R$/m³) do
    trimestre, do mesmo trimestre do ano anterior e do trimestre anterior."""
    import pdfplumber
    rx = re.compile(r"^(?:Margem )?EBITDA Ajustad[oa]\s*\(R\$/m.\)\s+(.*)$", re.I)
    with pdfplumber.open(io.BytesIO(pdf)) as p:
        for pg in p.pages[:20]:
            t = pg.extract_text() or ""
            if not re.search(r"distribui|mobilidade", t, re.I) or re.search(r"^\s*Argentina", t[:200], re.I | re.M):
                continue
            for line in t.split("\n"):
                m = rx.match(line.strip())
                if m:
                    rest = re.sub(r"^(\(\d\)\s*)+", "", m.group(1).strip())      # chamadas de nota, ex. "(2)"
                    nums = [x for x in rest.split() if re.fullmatch(r"\(?-?[\d.]+(,\d+)?\)?", x)]
                    if len(nums) >= 3:
                        def br(x):
                            v = float(x.strip("()").replace(".", "").replace(",", "."))
                            return -abs(v) if x.startswith("(") else v
                        return [br(x) for x in nums[:3]]
    return None


def _raizen_normalizado(pdf: bytes):
    """Margem "EBITDA Normalizado" (R$/m³) de Distribuição Brasil, publicada entre 3T 22'23 e 2025:
    a Raízen tira efeitos de estoque, CBIOs e créditos tributários do EBITDA ajustado. Devolve
    [trimestre, ano anterior, trimestre anterior] (None onde o release não traz)."""
    import pdfplumber
    tok = lambda s: [x for x in s.split() if re.fullmatch(r"\(?-?[\d.]+(,\d+)?\)?", x)]
    def br(x):
        v = float(x.strip("()").replace(".", "").replace(",", "."))
        return -abs(v) if x.startswith("(") else v
    with pdfplumber.open(io.BytesIO(pdf)) as p:
        for pg in p.pages[:22]:
            t = pg.extract_text() or ""
            if "ormaliza" not in t or re.search(r"argentina", t[:300], re.I):
                continue
            ls = [l.strip() for l in t.split("\n")]
            i0 = next((i for i, l in enumerate(ls) if re.match(r"^Margem \(R\$/m", l)), None)
            if i0 is not None:
                for l in ls[i0 + 1:]:
                    m = re.match(r"^EBITDA Normalizado(?:/m.)?\s+(.*)$", l)
                    if m and len(tok(m.group(1))) >= 3:
                        n = tok(m.group(1))
                        return [br(n[0]), br(n[1]), br(n[2])]
            for l in ls:
                m = re.match(r"^EBITDA Normalizado(?: \(R\$ milh.es\))?\s+(.*)$", l)
                if m and len(tok(m.group(1))) == 4:
                    return [br(tok(m.group(1))[2]), None, None]
    return None


RAIZEN_REL_CAT = "crrelatorioresultadosgruporaizendfp"


def _raizen_releases(get, cache: dict) -> dict:
    """Relatórios de resultados da Raízen (API do site de RI). Por trimestre civil, a prioridade é:
    EBITDA normalizado do próprio release > normalizado dos comparativos > margem ajustada do próprio
    release > ajustada dos comparativos. Os resultados brutos de cada release ficam em cache."""
    y = dt.date.today().year
    url = RAIZEN_API.format(site=RAIZEN_SITE) + f"?language=pt&categories={RAIZEN_REL_CAT}&startDate=2021-01-01&endDate={y + 2}-12-31"
    files = json.loads(get(url).content)["categories"].get(RAIZEN_REL_CAT, [])
    brutos = cache.setdefault("brutos", {})
    for f in files:
        ref = f["refDate"][:10]
        if ref in brutos:
            continue
        try:
            pdf = get(f["url"]).content
            brutos[ref] = {"aj": _raizen_release(pdf), "norm": _raizen_normalizado(pdf)}
        except Exception as e:  # noqa: BLE001
            print(f"Raízen: release {ref} falhou: {str(e)[:120]}")
    cand = {}                                      # tri -> (prioridade, ordem, valor); menor prioridade vence
    for ref in sorted(brutos):
        fy, fq = int(ref[:4]), (int(ref[5:7]) + 2) // 3
        q = f"{fy - 1}-T{fq + 1}" if fq <= 3 else f"{fy}-T1"
        for tipo, base in (("norm", 0), ("aj", 2)):
            v = brutos[ref].get(tipo)
            if not v:
                continue
            for k, (tri, x) in enumerate(((q, v[0]), (_tri_menos(q, 4), v[1]), (_tri_menos(q, 1), v[2]))):
                if x is None:
                    continue
                pr = base + (0 if k == 0 else 1)
                antigo = cand.get(tri)
                if antigo is None or pr < antigo[0] or (pr == antigo[0] and k > 0):   # comparativo mais novo revisa
                    cand[tri] = (pr, tipo, x)
    print(f"Raízen: {len(files)} releases listados, {len(brutos)} lidos, {len(cand)} trimestres")
    return {t: {"valor": x, "tipo": tipo} for t, (pr, tipo, x) in cand.items()}


def _ipiranga(get, cache: dict, max_downloads=None) -> dict:
    import os
    max_downloads = int(os.environ.get("IPIRANGA_MAX_DOWNLOADS", "40")) if max_downloads is None else max_downloads
    import csv
    import zipfile
    y = dt.date.today().year
    rel = {}
    for ano in range(2019, y + 1):
        try:
            z = zipfile.ZipFile(io.BytesIO(get(CVM_IPE.format(ano=ano)).content))
        except Exception as e:  # noqa: BLE001
            print(f"Ipiranga: IPE {ano} falhou: {e}")
            continue
        for name in z.namelist():
            for r in csv.DictReader(io.StringIO(z.read(name).decode("latin1")), delimiter=";"):
                if r["Codigo_CVM"] == ULTRAPAR_CVM and r["Tipo"] == "Press-release":
                    d = r["Data_Referencia"]
                    if d not in rel or int(r["Versao"]) > int(rel[d]["Versao"]):
                        rel[d] = r
    vals = dict(cache.get("valores", {}))
    recs = set(cache.get("recorrente", []))           # trimestres com a linha recorrente do release
    feitos = set(cache.get("releases", []))
    n = 0
    for d in sorted(rel, reverse=True):                 # mais recentes primeiro
        if d in feitos or n >= max_downloads:
            continue
        n += 1
        try:
            res = _ipiranga_release(get(rel[d]["Link_Download"]).content)
        except Exception as e:  # noqa: BLE001
            print(f"Ipiranga: release {d} falhou: {str(e)[:120]}")
            continue
        feitos.add(d)
        if res:
            res, is_rec = res
            for i, (q, v) in enumerate(res.items()):
                cur = i == 0                            # o trimestre do próprio release
                if is_rec and (cur or q not in recs):   # recorrente sempre vence o cálculo ajustado
                    vals[q] = v
                    recs.add(q)
                elif not is_rec and q not in recs and (cur or q not in vals):
                    vals[q] = v
    print(f"Ipiranga: {len(rel)} releases listados, {n} baixados, {len(vals)} trimestres")
    cache["valores"], cache["releases"], cache["recorrente"] = vals, sorted(feitos), sorted(recs)
    serie = [{"tri": q, "rotulo": f"{q[-1]}T{q[2:4]}", "valor": vals[q]} for q in sorted(vals)]
    return {"arquivo": "releases de resultados da Ultrapar (CVM)", "data_arquivo": max(feitos) if feitos else "", "serie": serie}


def build(get, post, cache_ipiranga: dict | None = None, cache_raizen: dict | None = None) -> dict:
    out = {}
    if cache_ipiranga is not None:
        try:
            out["Ipiranga"] = _ipiranga(get, cache_ipiranga)
        except Exception as e:  # noqa: BLE001
            print("Ipiranga EBITDA/m³ falhou:", e)
    try:
        out["Raízen"] = _raizen(get, cache_raizen)
    except Exception as e:  # noqa: BLE001  (uma empresa falhar não derruba as outras)
        print("Raízen EBITDA/m³ falhou:", e)
    out["Vibra"] = _vibra(get, post)
    return out
