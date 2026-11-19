"""Produção mensal divulgada pelas próprias empresas (PRIO, Brava, PetroReconcavo).

As três publicam na CVM, no começo de cada mês, um comunicado com a produção do mês anterior
(participação da empresa, boe/d). Esses números saem uns 40 dias antes do arquivo por poço da ANP,
então servem para mostrar o trimestre corrente. Cada comunicado lido fica em cache pelo protocolo.
"""
from __future__ import annotations

import csv
import datetime as dt
import io
import re
import zipfile

CVM_IPE = "https://dados.cvm.gov.br/dados/CIA_ABERTA/DOC/IPE/DADOS/ipe_cia_aberta_{ano}.zip"
EMPRESAS = {"22187": "PRIO", "25291": "Brava Energia", "25780": "PetroReconcavo"}
ASSUNTO = re.compile(r"Dados\s+(Operacionais|de\s+Produ)|Comunicado\s+de\s+Produ", re.I)
MESES = ["janeiro", "fevereiro", "março", "abril", "maio", "junho", "julho", "agosto", "setembro",
         "outubro", "novembro", "dezembro"]
ABREV = {m[:3].upper(): i + 1 for i, m in enumerate(MESES)}
ANO_INICIO = 2023


def _n(t: str):
    t = t.strip()
    if t in ("-", "–"):
        return 0.0
    if not re.fullmatch(r"\d{1,3}(\.\d{3})*(,\d+)?|\d+(,\d+)?", t):
        return None
    return float(t.replace(".", "").replace(",", "."))


def _texto(pdf: bytes) -> str:
    import pdfplumber
    with pdfplumber.open(io.BytesIO(pdf)) as p:
        return "\n".join((pg.extract_text() or "") for pg in p.pages[:3])


def _ano_titulo(t: str) -> int | None:
    m = re.search(r"(?:DE|–|-)\s*(" + "|".join(MESES) + r")\s*(?:/|DE\s*)?\s*(20\d\d|\d\d)\b", t[:600], re.I)
    if not m:
        return None
    a = int(m.group(2))
    return a + 2000 if a < 100 else a


def _mes_linhas(t: str, ano: int, ler):
    """Linhas que começam com o nome do mês (tabelas de PRIO e PetroReconcavo)."""
    out = {}
    for ln in t.split("\n"):
        m = re.match(r"^(" + "|".join(MESES) + r")\s+(.*)$", ln.strip(), re.I)
        if not m:
            continue
        mes = MESES.index(m.group(1).lower()) + 1
        toks = [x for x in re.sub(r"\(\d\)", " ", m.group(2)).split()]
        vals = [_n(x) for x in toks]
        if not vals or any(v is None for v in vals):
            continue
        r = ler(vals)
        if r and f"{ano}-{mes:02d}" not in out:   # primeira tabela do documento = produção
            out[f"{ano}-{mes:02d}"] = r
    return out


def _prio(t: str) -> dict:
    ano = _ano_titulo(t)
    if not ano:
        return {}
    t = t.split("Vendas")[0]

    def ler(v):
        if len(v) < 3 or len(v) % 2 == 0:
            return None
        oil, gas, tot = sum(v[:-1:2]), sum(v[1:-1:2]), v[-1]
        if abs(oil + gas - tot) > max(50, 0.01 * tot):
            return None
        return {"oil": oil, "gas": gas, "boe": tot}
    return _mes_linhas(t, ano, ler)


def _recv(t: str) -> dict:
    ano = _ano_titulo(t)
    if not ano:
        return {}
    t = re.split(r"Operational Data", t)[0]

    def ler(v):
        # ... Total: óleo, gás, total (boe/d) seguidos da entrega do mês (3 números)
        if len(v) < 6:
            return None
        oil, gas, tot = v[-6], v[-5], v[-4]
        if abs(oil + gas - tot) > 3 or tot < 1000:
            return None
        return {"oil": oil, "gas": gas, "boe": tot}
    out = _mes_linhas(t, ano, ler)
    if not out:   # tabela em imagem: usa o texto ("registrada em outubro foi de 22,5 mil boe/dia")
        m = re.search(r"registrada em (\w+) foi de ([\d,]+) mil boe", t, re.I)
        if m and m.group(1).lower() in MESES:
            oil = sum(float(x.replace(",", ".")) for x in re.findall(r"produ[çc][ãa]o de petr[óo]leo de ([\d,]+) mil", t))
            gas = sum(float(x.replace(",", ".")) for x in re.findall(r"e ([\d,]+) mil boe/dia de g[áa]s", t))
            out[f"{ano}-{MESES.index(m.group(1).lower()) + 1:02d}"] = {
                "boe": float(m.group(2).replace(",", ".")) * 1000, "oil": oil * 1000 or None, "gas": gas * 1000 or None}
    return out


def _brava(t: str) -> dict:
    lin = t.split("\n")
    hdr = next((l for l in lin if re.search(r"boe/d", l, re.I) and re.search(r"\b(" + "|".join(ABREV) + r")\b", l)), None)
    if not hdr:
        return {}
    toks = hdr.split("boe/d", 1)[-1].split()
    cols, i = [], 0
    while i < len(toks):
        a = toks[i].upper()
        if a[:3] in ABREV and i + 1 < len(toks) and re.fullmatch(r"\d\d", toks[i + 1]):
            cols.append(f"20{toks[i + 1]}-{ABREV[a[:3]]:02d}")
            i += 2
        else:
            cols.append(None)
            i += 1

    def linha(rx):
        l = next((l for l in lin if re.match(rx, l.strip(), re.I)), None)
        if not l:
            return None
        v = [_n(x) for x in l.split()]
        v = [x for x in v if x is not None]
        return v[-len(cols):] if len(v) >= len(cols) else None
    tot, oil, gas = linha(r"^Produ[çc][ãa]o\s+Total"), linha(r"^[ÓO]leo\s*\|"), linha(r"^G[áa]s\s*\|")
    if not tot:
        return {}
    out = {}
    for j, c in enumerate(cols):
        if c:
            out[c] = {"boe": tot[j], "oil": oil[j] if oil else None, "gas": gas[j] if gas else None}
    return out


PARSER = {"PRIO": _prio, "Brava Energia": _brava, "PetroReconcavo": _recv}


def build(get, cache: dict) -> dict:
    """cache: {"docs": {protocolo: {"empresa", "entrega", "meses": {...}}}}."""
    docs = cache.setdefault("docs", {})
    alvo = []
    for ano in range(ANO_INICIO, dt.date.today().year + 1):
        try:
            z = zipfile.ZipFile(io.BytesIO(get(CVM_IPE.format(ano=ano)).content))
        except Exception as e:  # noqa: BLE001
            print(f"IPE {ano} falhou: {e}")
            continue
        for name in z.namelist():
            for r in csv.DictReader(io.StringIO(z.read(name).decode("latin1")), delimiter=";"):
                emp = EMPRESAS.get(r["Codigo_CVM"])
                if emp and ASSUNTO.search(r["Assunto"] or "") and r["Categoria"].startswith("Comunicado"):
                    alvo.append((r["Data_Entrega"], emp, r["Protocolo_Entrega"], r["Link_Download"]))
    lidos = falhas = 0
    # mais recentes primeiro: o último comunicado de PRIO e PetroReconcavo já traz o ano inteiro
    for entrega, emp, prot, url in sorted(alvo, reverse=True):
        if prot in docs and docs[prot].get("meses"):
            continue
        if lidos >= 20 or falhas >= 6:     # o site da CVM é instável; o resto entra nas próximas rodadas
            break
        lidos += 1
        try:
            pdf = get(url).content
            if not pdf.startswith(b"%PDF"):
                raise RuntimeError("não é PDF")
            meses = PARSER[emp](_texto(pdf))
        except Exception as e:  # noqa: BLE001
            falhas += 1
            print(f"comunicado {emp} {entrega} falhou: {str(e)[:80]}")
            continue
        docs[prot] = {"empresa": emp, "entrega": entrega, "meses": meses}
    # documento mais recente prevalece (revisões)
    serie = {e: {} for e in PARSER}
    fonte = {e: {} for e in PARSER}
    for d in sorted(docs.values(), key=lambda d: d["entrega"]):
        for m, v in d["meses"].items():
            serie[d["empresa"]][m] = {k: (round(x) if x is not None else None) for k, x in v.items()}
            fonte[d["empresa"]][m] = d["entrega"]
    return {"empresas": {e: dict(sorted(s.items())) for e, s in serie.items()},
            "ultimo_comunicado": {e: max(f.values()) if f else None for e, f in fonte.items()}}
