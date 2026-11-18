#!/usr/bin/env python3
"""Coletor do dashboard de O&G Brasil.

Baixa todas as fontes públicas, calcula paridade de importação, defasagem e
crack spreads, e grava site/data/dashboard.json. Roda de hora em hora no
GitHub Actions. Nenhuma entrada manual.

Se uma fonte falhar, o bloco correspondente do último dashboard.json é mantido
e o erro fica registrado em site/data/meta.json.
"""
from __future__ import annotations

import csv
import datetime as dt
import io
import json
import math
import os
import re
import statistics
import sys
import time
import traceback
import unicodedata
import zipfile
from collections import defaultdict
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
SITE_DATA = ROOT / "site" / "data"
CACHE = ROOT / "data" / "cache"
REF = ROOT / "data" / "ref"
OUT = SITE_DATA / "dashboard.json"
META = SITE_DATA / "meta.json"

START_YEAR = 2015
CAL_WEEKS = int(os.environ.get("CAL_WEEKS", "4"))  # janela de calibração da PPI
BBL_PER_M3 = 6.28981
L_PER_BBL = 158.987
L_PER_GAL = 3.78541
GAL_PER_BBL = 42.0
CRUDE_KG_PER_BBL = 0.87 * L_PER_BBL  # densidade média ~0,87 t/m³

ANP = "https://www.gov.br/anp/pt-br"
ANP_DA = f"{ANP}/centrais-de-conteudo/dados-abertos"
ANP_DA_FILES = f"{ANP_DA}/arquivos"
ANP_PRECOS = f"{ANP}/assuntos/precos-e-defesa-da-concorrencia/precos"

S = requests.Session()
S.headers["User-Agent"] = "Mozilla/5.0 (compatible; og-dashboard/1.0)"

MESES = {"JAN": 1, "FEV": 2, "MAR": 3, "ABR": 4, "MAI": 5, "JUN": 6,
         "JUL": 7, "AGO": 8, "SET": 9, "OUT": 10, "NOV": 11, "DEZ": 12}

STATUS: dict[str, dict] = {}


# ----------------------------------------------------------------- helpers
def get(url, *, timeout=120, tries=3, **kw) -> requests.Response:
    last = None
    for i in range(tries):
        try:
            r = S.get(url, timeout=timeout, **kw)
            r.raise_for_status()
            return r
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(3 * (i + 1))
    raise RuntimeError(f"GET {url}: {last}")


def num_br(s) -> float:
    """'1.234,56' -> 1234.56 ; aceita números já float."""
    if s is None:
        return 0.0
    if isinstance(s, (int, float)):
        return float(s)
    s = str(s).strip()
    if not s or s in {"-", "***"}:
        return 0.0
    if "," in s:
        s = s.replace(".", "").replace(",", ".")
    try:
        return float(s)
    except ValueError:
        return 0.0


def days_in_month(y: int, m: int) -> int:
    nxt = dt.date(y + (m == 12), m % 12 + 1, 1)
    return (nxt - dt.date(y, m, 1)).days


def ym(y: int, m: int) -> str:
    return f"{y:04d}-{m:02d}"


def norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode()
    return re.sub(r"\s+", " ", s).strip().upper()


def r2(x, n=2):
    return None if x is None or (isinstance(x, float) and math.isnan(x)) else round(x, n)


def page_links(url: str, pattern: str) -> list[str]:
    html = get(url).text
    return sorted(set(re.findall(pattern, html)))


def section(name):
    """Decorator: executa um bloco e registra status; em erro devolve None."""
    def deco(fn):
        def run(*a, **kw):
            t0 = time.time()
            try:
                res = fn(*a, **kw)
                STATUS[name] = {"ok": True, "segundos": round(time.time() - t0, 1)}
                return res
            except Exception as e:  # noqa: BLE001
                traceback.print_exc()
                STATUS[name] = {"ok": False, "erro": str(e)[:300]}
                return None
        return run
    return deco


def cache_json(path: Path, max_age_h: float | None, builder):
    """Lê do cache se ainda for válido; senão reconstrói e grava.
    A idade vem do carimbo gravado no arquivo (o checkout do git não preserva
    a data de modificação)."""
    if path.exists() and max_age_h is not None:
        obj = json.loads(path.read_text())
        if isinstance(obj, dict) and "_ts" in obj:
            if (time.time() - obj["_ts"]) / 3600 < max_age_h:
                return obj["data"]
        elif max_age_h >= 1e8:  # formato antigo, sem carimbo, cache permanente
            return obj
    data = builder()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"_ts": int(time.time()), "data": data}, ensure_ascii=False, separators=(",", ":")))
    return data


# ------------------------------------------------------- daily market data
@section("eia")
def fetch_fred():
    """Brent, diesel USGC (ULSD) e gasolina USGC, diários. Fonte primária:
    planilhas históricas do EIA; reserva: as mesmas séries no FRED."""
    eia = {"RBRTEd": "brent", "RWTCd": "wti", "EER_EPD2DXL0_PF4_RGC_DPGd": "diesel_usgc",
           "EER_EPMRU_PF4_RGC_DPGd": "gasolina_usgc"}
    fred = {"brent": "DCOILBRENTEU", "wti": "DCOILWTICO", "diesel_usgc": "DDFUELUSGULF", "gasolina_usgc": "DGASUSGULF"}
    out = {}
    for code, key in eia.items():
        ser = {}
        try:
            import xlrd
            book = xlrd.open_workbook(file_contents=get(f"https://www.eia.gov/dnav/pet/hist_xls/{code}.xls").content)
            sh = book.sheet_by_name("Data 1")
            for i in range(3, sh.nrows):
                d, v = sh.cell_value(i, 0), sh.cell_value(i, 1)
                if isinstance(d, float) and isinstance(v, float):
                    day = xlrd.xldate_as_datetime(d, book.datemode).date().isoformat()
                    if day >= f"{START_YEAR}-01-01":
                        ser[day] = v
        except Exception as e:  # noqa: BLE001
            print(f"EIA {code} falhou ({e}); tentando FRED")
        if len(ser) < 100:
            txt = requests.get("https://fred.stlouisfed.org/graph/fredgraph.csv", timeout=90,
                               params={"id": fred[key], "cosd": f"{START_YEAR}-01-01"}).text
            for row in csv.reader(io.StringIO(txt)):
                if len(row) >= 2 and re.match(r"\d{4}-\d{2}-\d{2}", row[0]):
                    try:
                        ser[row[0]] = float(row[1])
                    except ValueError:
                        pass
        if len(ser) < 100:
            raise RuntimeError(f"série {key} vazia no EIA e no FRED")
        out[key] = ser
    return out


@section("bcb_ptax")
def fetch_ptax():
    """USD/BRL PTAX venda (SGS série 1). Janelas de até 10 anos."""
    ser = {}
    today = dt.date.today()
    starts = [dt.date(START_YEAR, 1, 1), dt.date(START_YEAR + 9, 1, 1)]
    for i, a in enumerate(starts):
        b = (starts[i + 1] - dt.timedelta(days=1)) if i + 1 < len(starts) else today
        r = get("https://api.bcb.gov.br/dados/serie/bcdata.sgs.1/dados",
                params={"formato": "json", "dataInicial": a.strftime("%d/%m/%Y"),
                        "dataFinal": b.strftime("%d/%m/%Y")})
        for p in r.json():
            d = dt.datetime.strptime(p["data"], "%d/%m/%Y").date().isoformat()
            ser[d] = float(p["valor"])
    if len(ser) < 100:
        raise RuntimeError("PTAX vazia")
    return ser


# --------------------------------------------------------- ANP: production
def _state_csv(name):
    txt = get(f"{ANP_DA_FILES}/ppgn-el/{name}").content.decode("utf-8-sig")
    tot, loc = defaultdict(float), defaultdict(float)
    for r in csv.DictReader(io.StringIO(txt), delimiter=";"):
        y = int(r["ANO"])
        if y < START_YEAR:
            continue
        k = ym(y, MESES[r["MÊS"].strip().upper()])
        v = num_br(r["PRODUÇÃO"])
        tot[k] += v
        loc[(k, r["LOCALIZAÇÃO"].strip().upper())] += v
    return tot, loc


@section("anp_producao_estado")
def fetch_production_state():
    """Produção oficial mensal por localização (mar/terra). Petróleo em m³,
    gás em mil m³. Convertido para bbl/d e milhões m³/d."""
    oil_t, oil_l = _state_csv("producao-petroleo-m3.csv")
    gas_t, gas_l = _state_csv("producao-gas-natural-1000m3.csv")
    months = sorted(k for k, v in oil_t.items() if v > 0)
    out = []
    for k in months:
        y, m = map(int, k.split("-"))
        d = days_in_month(y, m)
        out.append({
            "mes": k,
            "petroleo_mar": r2(oil_l[(k, "MAR")] * BBL_PER_M3 / d, 0),
            "petroleo_terra": r2(oil_l[(k, "TERRA")] * BBL_PER_M3 / d, 0),
            "petroleo": r2(oil_t[k] * BBL_PER_M3 / d, 0),
            "gas": r2(gas_t[k] / 1000 / d, 2),
            "gas_mar": r2(gas_l[(k, "MAR")] / 1000 / d, 2),
            "gas_terra": r2(gas_l[(k, "TERRA")] / 1000 / d, 2),
        })
    # descarta meses finais incompletos (ANP às vezes publica zeros)
    while out and out[-1]["petroleo"] < 0.5 * out[-2]["petroleo"]:
        out.pop()
    return out


PRESAL_RX = re.compile(r"(BVE|ITP|MCB|COQ|PIC|BARRA VELHA|ITAPEMA|MACABU)", re.I)


def _field_name(c: str) -> str:
    c = re.sub(r"^AnC_", "", c.strip(), flags=re.I)
    c = re.sub(r"_(ECO|EXC|PP\d?|P\d)$", "", c, flags=re.I)
    return c.strip().upper()


def _zona_month(url: str) -> dict:
    txt = get(url, timeout=180).content.decode("utf-8-sig", errors="replace")
    rows = csv.DictReader(io.StringIO(txt))
    fields: dict[str, dict] = defaultdict(lambda: {"oil": 0.0, "gas": 0.0, "env": ""})
    tot_oil = tot_gas = pre_oil = pre_gas = 0.0
    for r in rows:
        oil = num_br(r.get("Petróleo (m³/d)"))
        gas = num_br(r.get("Gás total (Mil m³/d)"))
        if "Pré-sal" in r and r["Pré-sal"] in ("S", "N"):
            pre = r["Pré-sal"] == "S"
        else:
            pre = bool(PRESAL_RX.search(r.get("Zona") or ""))
        f = fields[_field_name(r.get("Campo", ""))]
        f["oil"] += oil
        f["gas"] += gas
        f["env"] = (r.get("Ambiente") or "").upper()
        f["bacia"] = r.get("Bacia") or ""
        tot_oil += oil
        tot_gas += gas
        if pre:
            pre_oil += oil
            pre_gas += gas
    if tot_oil <= 0:
        raise RuntimeError(f"zona vazia: {url}")
    # guardamos só participações (os arquivos antigos têm escala diferente;
    # os volumes absolutos vêm da série oficial por estado)
    return {
        "presal_share_oil": pre_oil / tot_oil,
        "presal_share_gas": pre_gas / tot_gas if tot_gas else None,
        "fields": {k: {"s": v["oil"] / tot_oil, "env": v["env"], "bacia": v.get("bacia", "")}
                   for k, v in fields.items() if v["oil"] > 0},
    }


@section("anp_producao_campos")
def fetch_production_fields():
    """Arquivos mensais de produção por zona/poço (ANP), 2023+.
    Cache por mês; só baixa meses novos e revalida os 2 últimos a cada 24h."""
    links = page_links(f"{ANP_DA}/fase-de-desenvolvimento-e-producao",
                       r'href="([^"]*producao-por-zona/[^"]+\.csv)"')
    by_month = {}
    for u in links:
        m = re.search(r"(\d{2})[-_]?(\d{4})\.csv$", u, re.I)
        if m:
            by_month[ym(int(m.group(2)), int(m.group(1)))] = u
    if not by_month:
        raise RuntimeError("nenhum arquivo de produção por zona encontrado")
    months = sorted(by_month)
    recent = set(months[-2:])
    out = {}
    for k in months:
        p = CACHE / "zona" / f"{k}.json"
        out[k] = cache_json(p, 24 if k in recent else 1e9, lambda u=by_month[k]: _zona_month(u))
    return out


@section("anp_operadores")
def fetch_field_operator():
    """Mapa campo -> operador. O último arquivo da ANP com coluna 'Operador'
    é o de produção por poço de dez/2023; é baixado uma vez e guardado."""
    p = REF / "campo_operador.json"
    if p.exists():
        return json.loads(p.read_text())
    url = f"{ANP_DA_FILES}/arquivos-producao-de-petroleo-e-gas-natural-por-poco/2023/producao-12.zip"
    z = zipfile.ZipFile(io.BytesIO(get(url, timeout=180).content))
    best: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    for name in z.namelist():
        if not name.lower().endswith(".csv") or "presal" in name.lower():
            continue
        txt = z.read(name).decode("latin1")
        for r in csv.DictReader(io.StringIO(txt), delimiter=";"):
            best[_field_name(r["Campo"])][r["Operador"].strip()] += num_br(r.get("Petróleo (bbl/dia)")) + 1e-9
    mp = {f: max(ops.items(), key=lambda t: t[1])[0] for f, ops in best.items()}
    REF.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(mp, ensure_ascii=False, indent=0, sort_keys=True))
    return mp


# Campos que começaram a produzir depois de dez/2023 (sem operador no arquivo
# de referência da ANP). Lista fixa, não é entrada manual recorrente.
OPERADOR_EXTRA = {"BACALHAU": "Equinor", "BACALHAU NORTE": "Equinor"}


def operator_group(name: str) -> str:
    n = norm(name)
    rules = [("PETROBRAS", "Petrobras"), ("PRIO", "PRIO"), ("PETRO RIO", "PRIO"),
             ("SHELL", "Shell"), ("TOTALENERGIES", "TotalEnergies"), ("EQUINOR", "Equinor"),
             ("3R", "Brava Energia"), ("ENAUTA", "Brava Energia"), ("BRAVA", "Brava Energia"),
             ("TRIDENT", "Trident"), ("KAROON", "Karoon"), ("PERENCO", "Perenco"),
             ("PETRORECONCAVO", "PetroReconcavo"), ("BW ", "BW Energy"), ("CHEVRON", "Chevron"),
             ("PETRONAS", "Petronas"), ("REPSOL", "Repsol Sinopec"), ("EXXON", "ExxonMobil"),
             ("ENEVA", "Eneva"), ("ORIGEM", "Origem")]
    for k, v in rules:
        if k in n:
            return v
    return name


# --------------------------------------------------------- ANP: fuel sales
@section("anp_vendas")
def fetch_sales():
    links = page_links(f"{ANP_DA}/vendas-de-derivados-de-petroleo-e-biocombustiveis",
                       r'href="([^"]*vendas-derivados-petroleo-e-etanol/vendas-combustiveis-m3[^"]*\.csv)"')
    if not links:
        raise RuntimeError("link de vendas não encontrado")
    txt = get(links[-1], timeout=240).content.decode("utf-8-sig")
    want = {"ÓLEO DIESEL": "diesel", "GASOLINA C": "gasolina_c",
            "ETANOL HIDRATADO": "etanol", "GLP": "glp", "QUEROSENE DE AVIAÇÃO": "qav"}
    agg = defaultdict(lambda: defaultdict(float))
    for r in csv.DictReader(io.StringIO(txt), delimiter=";"):
        y = int(r["ANO"])
        p = want.get(r["PRODUTO"].strip().upper())
        if y < START_YEAR or not p:
            continue
        agg[ym(y, MESES[r["MÊS"].strip().upper()])][p] += num_br(r["VENDAS"])
    out = []
    for k in sorted(agg):
        out.append({"mes": k, **{p: r2(v / 1000, 1) for p, v in agg[k].items()}})  # mil m³
    while out and (out[-1].get("diesel") or 0) < 0.5 * (out[-2].get("diesel") or 1):
        out.pop()
    return out


# ------------------------------------------------------- ANP: pump prices
@section("anp_precos_bomba")
def fetch_pump_prices():
    import openpyxl
    url = f"{ANP_PRECOS}/precos-revenda-e-de-distribuicao-combustiveis/shlp/semanal/semanal-brasil-desde-2013.xlsx"
    wb = openpyxl.load_workbook(io.BytesIO(get(url).content), read_only=True, data_only=True)
    ws = wb.worksheets[0]
    want = {"OLEO DIESEL S10": "diesel_s10", "OLEO DIESEL": "diesel",
            "GASOLINA COMUM": "gasolina", "ETANOL HIDRATADO": "etanol", "GLP": "glp"}
    weekly = []
    monthly = defaultdict(lambda: defaultdict(list))
    for r in ws.iter_rows(values_only=True):
        if not r or not isinstance(r[0], dt.datetime) or r[2] not in want:
            continue
        price = r[5]
        if not isinstance(price, (int, float)):
            continue
        k = want[r[2]]
        d_end = r[1].date()
        weekly.append((d_end.isoformat(), k, price))
        if d_end.year >= START_YEAR:
            monthly[ym(d_end.year, d_end.month)][k].append(price)
    out_m = [{"mes": k, **{p: r2(statistics.mean(v), 3) for p, v in monthly[k].items()}}
             for k in sorted(monthly)]
    last = {}
    for d, k, p in weekly:
        last[k] = (d, p)
    return {"mensal": out_m, "ultima_semana": {k: {"data": d, "preco": p} for k, (d, p) in last.items()}}


# ----------------------------------------------- ANP: import parity (PPI)
@section("anp_ppi")
def fetch_anp_ppi():
    """PPI semanal oficial da ANP (Santos). Usado só para calibrar custos."""
    import openpyxl
    links = page_links(f"{ANP_PRECOS}/precos-de-paridade-de-importacao",
                       r'href="([^"]*arq-ppi/ppi\.xlsx)"') or [f"{ANP_PRECOS}/arq-ppi/ppi.xlsx"]
    wb = openpyxl.load_workbook(io.BytesIO(get(links[0]).content), read_only=True, data_only=True)
    out = {}
    for ws in wb.worksheets:
        t = ws.title.lower()
        key = "diesel" if t.startswith("diesel") else "gasolina" if t.startswith("gasolina") else None
        if not key or "r$" not in t:
            continue
        col = None
        ser = {}
        for r in ws.iter_rows(values_only=True):
            if col is None:
                for i, c in enumerate(r):
                    if isinstance(c, str) and c.strip().lower() == "santos":
                        col = i  # primeira ocorrência = bloco de preços (R$/L)
                        break
                continue
            lab = r[1]
            if not isinstance(lab, str):
                continue
            m = re.findall(r"(\d{2}/\d{2}/\d{4})", lab)
            v = r[col] if col is not None and col < len(r) else None
            if len(m) == 2 and isinstance(v, (int, float)):
                a = dt.datetime.strptime(m[0], "%d/%m/%Y").date()
                b = dt.datetime.strptime(m[1], "%d/%m/%Y").date()
                ser[a.isoformat()] = {"ini": a.isoformat(), "fim": b.isoformat(), "ppi": v}
        out[key] = ser
    if not out.get("diesel"):
        raise RuntimeError("PPI ANP sem diesel")
    return out


# -------------------------------------------- Petrobras: preços sem tributos
PB_LOCAL = "Cubatão (SP)"  # ponto de fornecimento vizinho ao porto de Santos
PB_MOD = "EXA"


def _pb_parse_pdf(content: bytes, want_titles: dict[str, str]) -> dict:
    import pdfplumber
    res: dict[str, dict] = {k: {"tabela": {}, "liquido": {}} for k in want_titles.values()}
    with pdfplumber.open(io.BytesIO(content)) as pdf:
        for page in pdf.pages:
            head = (page.extract_text() or "").split("\n")[0]
            key = next((v for k, v in want_titles.items() if k.lower() in head.lower()), None)
            if not key:
                continue
            for tb in page.extract_tables():
                if not tb or not tb[0] or (tb[0][0] or "").strip().upper() != "LOCAL":
                    continue
                hdr = tb[0]
                row = next((r for r in tb[1:] if (r[0] or "").strip() == PB_LOCAL
                            and (r[1] or "").strip() == PB_MOD), None)
                if not row:
                    continue
                for h, cell in zip(hdr[2:], row[2:]):
                    m = re.match(r"\s*(\d{2})\.(\d{2})\.(\d{4})", h or "")
                    v = num_br(cell)
                    if not m or v <= 0:
                        continue
                    d = dt.date(int(m.group(3)), int(m.group(2)), int(m.group(1))).isoformat()
                    if "desconto" in (h or "").lower():
                        res[key]["liquido"][d] = v / 1000
                    else:
                        res[key]["tabela"][d] = v / 1000
    return res


@section("petrobras_precos")
def fetch_petrobras():
    html = get("https://precos.petrobras.com.br/web/precos-dos-combustiveis").text
    m = re.search(r"var listaTabelaJson\s*=\s*(\[.*?\]);", html, re.S)
    if not m:
        raise RuntimeError("lista de tabelas da Petrobras não encontrada")
    tabs = json.loads(m.group(1))
    out = {}
    for t in tabs:
        tit = t["titulo"].lower()
        if "diesel s500 e s10" in tit:
            content = get("https://precos.petrobras.com.br" + t["urlTabelaPrecos"]).content
            out.update(_pb_parse_pdf(content, {"Diesel S10": "diesel"}))
        elif "gasolina" in tit:
            content = get("https://precos.petrobras.com.br" + t["urlTabelaPrecos"]).content
            out.update(_pb_parse_pdf(content, {"Gasolina A sem": "gasolina"}))
    for k in ("diesel", "gasolina"):
        if not out.get(k, {}).get("tabela"):
            raise RuntimeError(f"preço Petrobras {k} não lido")
    # composição do preço na bomba (semanal, página de formação de preços da Petrobras)
    parc, comp = {}, {}
    for prod in ("diesel", "gasolina"):
        try:
            h = get(f"https://precos.petrobras.com.br/precos-{prod}").text
            per = re.search(r'telafinal-textoColeta">\s*([^<]+)<', h)
            fin = re.search(r'telafinal-precofinal">\s*([\d,\.]+)\s*<', h)
            parts = {}
            for i in range(1, 8):
                t = re.search(rf'tarifa{i}-titulo">\s*([^<]+)<', h)
                n = re.search(rf'tarifa{i}-numero">\s*([\d,\.]+)\s*<', h)
                if t and n:
                    parts[t.group(1).strip()] = num_br(n.group(1))
            if "Parcela Petrobras" in parts:
                parc[prod] = {"parcela_rs_l": parts["Parcela Petrobras"], "periodo": (per.group(1).strip() if per else "")}
            if per and fin and parts:
                comp[prod] = {"periodo": per.group(1).strip(), "final": num_br(fin.group(1)), "partes": parts}
        except Exception:  # noqa: BLE001
            pass
    out["composicao_semana"] = comp
    out["parcela_bomba"] = parc
    return out


# ----------------------------------------------------- Comex Stat (trade)
COMEX = "https://api-comexstat.mdic.gov.br/general?language=pt"


def _comex_year(flow: str, ncm4: str, year: int):
    body = {"flow": flow, "monthDetail": True,
            "period": {"from": f"{year}-01", "to": f"{year}-12"},
            "filters": [{"filter": "heading", "values": [ncm4]}],
            "details": ["country"], "metrics": ["metricFOB", "metricKG"]}
    for i in range(4):
        r = S.post(COMEX, json=body, timeout=120)
        if r.status_code == 429:
            time.sleep(15 * (i + 1))
            continue
        r.raise_for_status()
        return r.json()["data"]["list"]
    raise RuntimeError("Comex Stat 429")


EUROPA = {"Países Baixos (Holanda)", "Espanha", "Portugal", "Reino Unido", "França", "Itália",
          "Alemanha", "Noruega", "Bélgica", "Suécia", "Dinamarca", "Polônia", "Grécia",
          "Finlândia", "Irlanda", "Lituânia", "Romênia", "Bulgária", "Croácia", "Malta"}


def _grp_export(c):
    if c == "China":
        return "China"
    if c == "Estados Unidos":
        return "EUA"
    if c in EUROPA:
        return "Europa"
    if c == "Índia":
        return "Índia"
    return "Outros"


def _grp_import(c):
    if c == "Estados Unidos":
        return "EUA"
    if c == "Rússia":
        return "Rússia"
    if c in EUROPA:
        return "Europa"
    if c in {"Índia", "Emirados Árabes Unidos", "Arábia Saudita", "Omã", "Kuwait", "Bahrein", "Catar"}:
        return "Oriente Médio e Índia"
    return "Outros"


@section("comex")
def fetch_comex():
    this_year = dt.date.today().year
    res = {}
    for flow, ncm, grp, key in [("export", "2709", _grp_export, "export_petroleo"),
                                 ("import", "2710", _grp_import, "import_derivados")]:
        agg = defaultdict(lambda: defaultdict(lambda: [0.0, 0.0]))
        for y in range(START_YEAR, this_year + 1):
            p = CACHE / "comex" / f"{flow}_{ncm}_{y}.json"
            rows = cache_json(p, 6 if y >= this_year - 1 else 1e9,
                              lambda f=flow, n=ncm, yy=y: _comex_year(f, n, yy))
            for r in rows:
                k = ym(int(r["year"]), int(r["monthNumber"]))
                g = grp(r["country"])
                agg[k][g][0] += float(r["metricFOB"])
                agg[k][g][1] += float(r["metricKG"])
            time.sleep(1)
        out = []
        for k in sorted(agg):
            y, m = map(int, k.split("-"))
            d = days_in_month(y, m)
            row = {"mes": k}
            for g, (fob, kg) in agg[k].items():
                if flow == "export":
                    row[g] = r2(kg / CRUDE_KG_PER_BBL / d / 1000, 1)  # mil bbl/d
                else:
                    row[g] = r2(fob / 1e6, 1)  # US$ milhões
                row["_fob_total"] = r2((row.get("_fob_total") or 0) + fob / 1e6, 1)
            out.append(row)
        res[key] = out
    return res


# ------------------------------------------- downstream: distribuidoras
@section("argentina")
def fetch_argentina():
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import argentina
    return cache_json(CACHE / "argentina.json", 12, lambda: argentina.build(lambda u: get(u, timeout=120).content))


@section("argentina_pozos")
def fetch_argentina_pozos():
    """Poço a poço (PetroDB/Hugging Face): só reconstrói quando o repositório muda."""
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import argentina_pozos as ap
    path = CACHE / "argentina_pozos.json"
    old = json.loads(path.read_text()) if path.exists() else None
    if old and time.time() - old.get("_ts", 0) < 6 * 3600:
        return old["data"]
    sha = ap.versao(lambda u: get(u, timeout=60))
    if old and old.get("sha") == sha:
        old["_ts"] = int(time.time())
        path.write_text(json.dumps(old, ensure_ascii=False))
        return old["data"]
    data = ap.build(lambda u: get(u, timeout=600))
    path.write_text(json.dumps({"_ts": int(time.time()), "sha": sha, "data": data}, ensure_ascii=False))
    return data


@section("ri_distribuidoras")
def fetch_ri_distribuidoras():
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import margens_distribuidoras as md
    post = lambda u, b: requests.post(u, json=b, timeout=60)
    ip_path = CACHE / "ipiranga_releases.json"

    def build():
        ip = json.loads(ip_path.read_text()) if ip_path.exists() else {}
        out = md.build(lambda u: get(u, timeout=180), post, ip)
        ip_path.write_text(json.dumps(ip, ensure_ascii=False, indent=1))
        return out
    return cache_json(CACHE / "ri_distribuidoras.json", 12, build)


@section("anp_distribuidoras")
def fetch_downstream():
    """Vendas por distribuidora, canais e fornecedores (ANP liquidos.zip, ~22 MB).
    O arquivo muda no máximo uma vez por mês; baixa de novo a cada 12h."""
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import downstream
    return cache_json(CACHE / "downstream.json", 12,
                      lambda: downstream.build(get(downstream.URL, timeout=600).content))


# ------------------------------------------------------------ analytics
def ffill_series(ser: dict[str, float], dates: list[str]) -> dict[str, float]:
    out, last = {}, None
    keys = sorted(ser)
    j = 0
    for d in dates:
        while j < len(keys) and keys[j] <= d:
            last = ser[keys[j]]
            j += 1
        if last is not None:
            out[d] = last
    return out


def step_price(points: dict[str, float], dates: list[str]) -> dict[str, float]:
    """Preço vigente em cada data (vigências da Petrobras)."""
    return ffill_series(points, dates)


def compute_parity(fred, ptax, anp_ppi, pb):
    """Paridade de importação própria + defasagem + cracks.

    PPI (R$/L) = (P_USGC [US$/gal] / 3,785 + k) × PTAX
    k (US$/L) = frete + seguro + perdas + custos portuários/AFRMM/armazenagem,
    calibrado automaticamente: mediana móvel de 26 semanas da diferença entre a
    PPI semanal da ANP (Santos) e o produto USGC × câmbio. Só usa semanas
    passadas, então não "copia" a ANP no dia a dia; as cotações diárias vêm do
    EIA e do Banco Central.
    """
    start = "2019-01-01"
    all_dates = sorted(d for d in fred["brent"] if d >= start)
    fx = ffill_series(ptax, all_dates)
    res = {"metodo": {}, "diario": {}, "semanal": [], "mensal_crack": []}
    k_hist = {}
    for prod, fkey in (("diesel", "diesel_usgc"), ("gasolina", "gasolina_usgc")):
        usgc = fred[fkey]
        weeks = sorted((anp_ppi or {}).get(prod, {}).values(), key=lambda w: w["ini"])
        ks = []  # (fim da semana, k)
        for w in weeks:
            ds = [d for d in usgc if w["ini"] <= d <= w["fim"] and d in fx]
            if not ds:
                continue
            p = statistics.mean(usgc[d] for d in ds) / L_PER_GAL
            f = statistics.mean(fx[d] for d in ds)
            ks.append((w["fim"], w["ppi"] / f - p))
        k_series = {}
        for i, (d, _) in enumerate(ks):
            window = [k for _, k in ks[max(0, i - CAL_WEEKS + 1): i + 1]]
            k_series[d] = statistics.median(window)
        k_hist[prod] = k_series
        res["metodo"][prod] = {
            "k_atual_usd_l": r2(list(k_series.values())[-1], 4) if k_series else None,
            "k_atual_usd_bbl": r2(list(k_series.values())[-1] * L_PER_BBL, 2) if k_series else None,
            "semanas_calibracao": len(ks),
        }
    # séries diárias
    days = [d for d in all_dates if d in fx and d in fred["diesel_usgc"] and d in fred["gasolina_usgc"]]
    rows = []
    for prod, fkey in (("diesel", "diesel_usgc"), ("gasolina", "gasolina_usgc")):
        kser = ffill_series(k_hist[prod], days) if k_hist[prod] else {}
        tab = step_price(pb[prod]["tabela"], days) if pb else {}
        liq_pts = dict(pb[prod]["tabela"]) if pb else {}
        if pb:
            liq_pts.update(pb[prod]["liquido"])
        liq = step_price(liq_pts, days) if pb else {}
        for d in days:
            if d not in kser:
                continue
            ppi = (fred[fkey][d] / L_PER_GAL + kser[d]) * fx[d]
            res["diario"].setdefault(d, {})
            row = res["diario"][d]
            row[f"ppi_{prod}"] = ppi
            if d in tab:
                row[f"pb_{prod}"] = tab[d]
                row[f"desc_{prod}"] = tab[d] / ppi - 1
            if d in liq:
                row[f"pbliq_{prod}"] = liq[d]
                row[f"descliq_{prod}"] = liq[d] / ppi - 1
    # semanal (sexta-feira de referência) para o gráfico
    wk = defaultdict(list)
    for d, row in res["diario"].items():
        dd = dt.date.fromisoformat(d)
        wk[(dd - dt.timedelta(days=dd.weekday())).isoformat()].append(row)
    anp_week = {}
    for prod in ("diesel", "gasolina"):
        for w in (anp_ppi or {}).get(prod, {}).values():
            dd = dt.date.fromisoformat(w["ini"])
            anp_week.setdefault((dd - dt.timedelta(days=dd.weekday())).isoformat(), {})[prod] = w["ppi"]
    for w in sorted(wk):
        rs = wk[w]
        o = {"semana": w}
        for f in ("ppi_diesel", "pb_diesel", "pbliq_diesel", "desc_diesel", "descliq_diesel",
                  "ppi_gasolina", "pb_gasolina", "pbliq_gasolina", "desc_gasolina", "descliq_gasolina"):
            v = [r[f] for r in rs if f in r]
            if v:
                o[f] = r2(statistics.mean(v), 4)
        for prod in ("diesel", "gasolina"):
            if prod in anp_week.get(w, {}):
                o[f"anp_ppi_{prod}"] = r2(anp_week[w][prod], 4)
        rows.append(o)
    res["semanal"] = rows
    # aderência do nosso cálculo à PPI da ANP nas últimas 104 semanas
    for prod in ("diesel", "gasolina"):
        e = [r[f"ppi_{prod}"] / r[f"anp_ppi_{prod}"] - 1 for r in rows[-104:]
             if r.get(f"anp_ppi_{prod}") and r.get(f"ppi_{prod}")]
        if e:
            res["metodo"][prod]["erro_medio_abs_vs_anp"] = r2(statistics.mean(abs(x) for x in e), 4)
            res["metodo"][prod]["semanas_comparadas"] = len(e)
    for prod in res["metodo"]:
        res["metodo"][prod]["janela_semanas"] = CAL_WEEKS
    # cracks mensais (US$/bbl)
    crk = defaultdict(lambda: defaultdict(list))
    for d in sorted(d for d in fred["brent"] if d >= f"{START_YEAR}-01-01"):
        b = fred["brent"][d]
        m = d[:7]
        if d in fred["diesel_usgc"]:
            crk[m]["usgc_diesel"].append(fred["diesel_usgc"][d] * GAL_PER_BBL - b)
        if d in fred["gasolina_usgc"]:
            crk[m]["usgc_gasolina"].append(fred["gasolina_usgc"][d] * GAL_PER_BBL - b)
        row = res["diario"].get(d)
        if row and d in fx:
            for prod in ("diesel", "gasolina"):
                if f"pb_{prod}" in row:
                    crk[m][f"br_{prod}"].append(row[f"pb_{prod}"] * L_PER_BBL / fx[d] - b)
                if f"pbliq_{prod}" in row and row[f"pbliq_{prod}"] != row.get(f"pb_{prod}"):
                    crk[m][f"brliq_{prod}"].append(row[f"pbliq_{prod}"] * L_PER_BBL / fx[d] - b)
    res["mensal_crack"] = [{"mes": m, **{k: r2(statistics.mean(v), 2) for k, v in crk[m].items()}}
                           for m in sorted(crk)]
    return res


def monthly_mean(ser: dict[str, float], start=f"{START_YEAR}-01"):
    acc = defaultdict(list)
    for d, v in ser.items():
        if d[:7] >= start:
            acc[d[:7]].append(v)
    return {m: statistics.mean(v) for m, v in sorted(acc.items())}


def pct(a, b):
    return None if a is None or not b else a / b - 1


def find(rows, key, val):
    return next((r for r in rows if r.get(key) == val), None)


def shift_month(k: str, n: int) -> str:
    y, m = map(int, k.split("-"))
    t = y * 12 + (m - 1) + n
    return ym(t // 12, t % 12 + 1)


# ------------------------------------------------------------------ main
def main():
    prev = json.loads(OUT.read_text()) if OUT.exists() else {}

    fred = fetch_fred()
    ptax = fetch_ptax()
    prod = fetch_production_state()
    zona = fetch_production_fields()
    opmap = fetch_field_operator()
    sales = fetch_sales()
    pump = fetch_pump_prices()
    anp_ppi = fetch_anp_ppi()
    pb = fetch_petrobras()
    comex = fetch_comex()
    dstream = fetch_downstream()
    arg = fetch_argentina()
    ri = fetch_ri_distribuidoras()
    argp = fetch_argentina_pozos()

    D = dict(prev)  # mantém blocos anteriores se algo falhar
    D["gerado_em"] = prev.get("gerado_em")

    # ---- mercado diário
    if fred and ptax:
        months = sorted(set(monthly_mean(fred["brent"])))
        fxm = monthly_mean(ptax)
        brm = monthly_mean(fred["brent"])
        D["mercado"] = {
            "brent_diario": [[d, v] for d, v in sorted(fred["brent"].items())],
            "wti_diario": [[d, v] for d, v in sorted(fred.get("wti", {}).items())],
            "ptax_diario": [[d, v] for d, v in sorted(ptax.items()) if d >= "2024-01-01"],
            "mensal": [{"mes": m, "brent": r2(brm.get(m)), "ptax": r2(fxm.get(m), 4),
                        "brent_brl_l": r2(brm[m] * fxm[m] / L_PER_BBL, 4) if m in fxm else None}
                       for m in months],
        }

    # ---- produção
    if prod:
        if zona:
            known = [(i, zona[r["mes"]]["presal_share_oil"]) for i, r in enumerate(prod) if r["mes"] in zona]
            for i, r in enumerate(prod):
                z = zona.get(r["mes"])
                if z:
                    sh = z["presal_share_oil"]
                else:  # mês sem arquivo por zona: interpola entre vizinhos
                    lo = [(j, v) for j, v in known if j < i]
                    hi = [(j, v) for j, v in known if j > i]
                    if not lo or not hi:
                        continue
                    (j0, v0), (j1, v1) = lo[-1], hi[0]
                    sh = v0 + (v1 - v0) * (i - j0) / (j1 - j0)
                    r["presal_interpolado"] = True
                r["presal_share"] = r2(sh, 4)
                r["petroleo_presal"] = r2(r["petroleo"] * sh, 0)
        D["producao"] = prod

    if zona and prod:
        prod_by = {r["mes"]: r for r in prod}
        zmonths = [m for m in sorted(zona) if m in prod_by]
        last = zmonths[-1]
        yago = shift_month(last, -12)

        def field_vol(m):
            tot = prod_by[m]["petroleo"]
            return {f: v["s"] * tot for f, v in zona[m]["fields"].items()}

        cur = field_vol(last)
        old = field_vol(yago) if yago in zona and yago in prod_by else {}
        top = sorted(cur.items(), key=lambda t: -t[1])[:15]
        D["campos"] = {
            "mes": last, "mes_comparacao": yago if old else None,
            "top": [{"campo": f.title(), "bacia": zona[last]["fields"][f].get("bacia", ""),
                     "operador": operator_group(OPERADOR_EXTRA.get(f) or (opmap or {}).get(f, "Não mapeado")),
                     "bbl_d": r2(v, 0), "bbl_d_ano_anterior": r2(old.get(f), 0) if old else None}
                    for f, v in top],
        }
        # operadores: mês mais recente + série anual (média dos meses)
        def by_op(m):
            acc = defaultdict(float)
            for f, v in field_vol(m).items():
                acc[operator_group(OPERADOR_EXTRA.get(f) or (opmap or {}).get(f, "Não mapeado"))] += v
            return acc
        cur_op = by_op(last)
        tot = sum(cur_op.values())
        main_ops = [o for o, _ in sorted(cur_op.items(), key=lambda t: -t[1])[:7]]
        series = []
        for m in zmonths:
            acc = by_op(m)
            row = {"mes": m}
            for o, v in acc.items():
                g = o if o in main_ops else "Outros"
                row[g] = r2((row.get(g) or 0) + v, 0)
            series.append(row)
        D["operadores"] = {
            "mes": last,
            "ranking": [{"operador": o, "bbl_d": r2(v, 0), "share": r2(v / tot, 4)}
                        for o, v in sorted(cur_op.items(), key=lambda t: -t[1])[:10]],
            "principais": main_ops + ["Outros"],
            "serie": series,
        }

    if sales:
        D["vendas"] = sales
    if pump:
        D["precos_bomba"] = pump
    if comex:
        D["comercio"] = comex
    if dstream:
        D["downstream"] = dstream
    if arg:
        D["argentina"] = arg
    if ri:
        D["ebitda_m3"] = ri
    if argp:
        D.setdefault("argentina", {})["pozos"] = argp
    # ---- composição do preço na bomba: série acumulada a cada semana nova
    comp_path = ROOT / "data" / "cache" / "composicao.json"
    hist = json.loads(comp_path.read_text()) if comp_path.exists() else {}
    for prod, c in ((pb or {}).get("composicao_semana") or {}).items():
        m = re.findall(r"(\d{2})/(\d{2})/(\d{4})", c["periodo"])
        if len(m) != 2:
            continue
        fim = f"{m[1][2]}-{m[1][1]}-{m[1][0]}"
        rows = {r["fim"]: r for r in hist.get(prod, [])}
        canon = {}
        for t, v in c["partes"].items():
            tn = t.lower()
            k = ("petrobras" if "petrobras" in tn else "distribuicao" if tn.startswith("distribui") else
                 "estadual" if "estadual" in tn else "federal" if "federa" in tn else
                 "biocombustivel" if ("biodiesel" in tn or "etanol" in tn) else None)
            if k:
                canon[k] = v
        rows[fim] = {"fim": fim, "ini": f"{m[0][2]}-{m[0][1]}-{m[0][0]}", "final": c["final"], **canon}
        hist[prod] = [rows[k] for k in sorted(rows)]
    if hist:
        comp_path.write_text(json.dumps(hist, ensure_ascii=False, indent=1))
        D["composicao"] = hist

    # ---- paridade, defasagem e cracks
    if fred and ptax and anp_ppi:
        par = compute_parity(fred, ptax, anp_ppi, pb)
        last_day = max(par["diario"])
        D["paridade"] = {
            "metodo": par["metodo"],
            "local_petrobras": f"{PB_LOCAL} – modalidade {PB_MOD}",
            "ultimo_dia": last_day,
            "ultimo": {k: r2(v, 4) for k, v in par["diario"][last_day].items()},
            "semanal": par["semanal"],
            "vigencias": {p: {"tabela": sorted(pb[p]["tabela"].items())[-6:],
                              "liquido": sorted(pb[p]["liquido"].items())[-6:]} for p in ("diesel", "gasolina")} if pb else prev.get("paridade", {}).get("vigencias"),
            "parcela_bomba": (pb or {}).get("parcela_bomba"),
        }
        D["cracks"] = par["mensal_crack"]

    # ---- KPIs
    k = {}
    if D.get("producao"):
        P = D["producao"]
        cur = P[-1]
        prv = find(P, "mes", shift_month(cur["mes"], -1))
        yoy = find(P, "mes", shift_month(cur["mes"], -12))
        k["producao"] = {"mes": cur["mes"], "petroleo": cur["petroleo"], "gas": cur["gas"],
                         "petroleo_mm": r2(pct(cur["petroleo"], prv and prv["petroleo"]), 4),
                         "petroleo_aa": r2(pct(cur["petroleo"], yoy and yoy["petroleo"]), 4),
                         "gas_mm": r2(pct(cur["gas"], prv and prv["gas"]), 4),
                         "gas_aa": r2(pct(cur["gas"], yoy and yoy["gas"]), 4)}
        ps = [r for r in P if r.get("presal_share") is not None]
        if ps:
            k["presal"] = {"mes": ps[-1]["mes"], "share": ps[-1]["presal_share"],
                           "share_aa": (find(ps, "mes", shift_month(ps[-1]["mes"], -12)) or {}).get("presal_share")}
    if fred:
        b = sorted(fred["brent"].items())
        d0, v0 = b[-1]
        def back(days):
            target = (dt.date.fromisoformat(d0) - dt.timedelta(days=days)).isoformat()
            c = [v for d, v in b if d <= target]
            return c[-1] if c else None
        k["brent"] = {"data": d0, "valor": v0, "var_7d": r2(pct(v0, back(7)), 4),
                      "var_30d": r2(pct(v0, back(30)), 4), "var_1a": r2(pct(v0, back(365)), 4)}
    if ptax:
        p = sorted(ptax.items())
        k["ptax"] = {"data": p[-1][0], "valor": p[-1][1]}
    if D.get("comercio", {}).get("export_petroleo"):
        E = D["comercio"]["export_petroleo"]
        cur = E[-1]
        tot = sum(v for kk, v in cur.items() if kk not in ("mes", "_fob_total") and v)
        yo = find(E, "mes", shift_month(cur["mes"], -12))
        tot_yo = sum(v for kk, v in (yo or {}).items() if kk not in ("mes", "_fob_total") and v) if yo else None
        k["exportacao"] = {"mes": cur["mes"], "mil_bbl_d": r2(tot, 0), "usd_mi": cur.get("_fob_total"),
                           "aa": r2(pct(tot, tot_yo), 4)}
    if D.get("precos_bomba"):
        u = D["precos_bomba"]["ultima_semana"]
        k["bomba"] = {"diesel_s10": u.get("diesel_s10"), "gasolina": u.get("gasolina")}
    if D.get("paridade"):
        u = D["paridade"]["ultimo"]
        k["defasagem"] = {"data": D["paridade"]["ultimo_dia"],
                          "diesel": u.get("desc_diesel"), "diesel_liquido": u.get("descliq_diesel"),
                          "gasolina": u.get("desc_gasolina")}
    D["kpis"] = k

    # só atualiza 'gerado_em' se os dados mudaram
    core = {kk: v for kk, v in D.items() if kk != "gerado_em"}
    prev_core = {kk: v for kk, v in prev.items() if kk != "gerado_em"}
    if core != prev_core:
        D["gerado_em"] = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    SITE_DATA.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(D, ensure_ascii=False, separators=(",", ":")))
    META.write_text(json.dumps({
        "verificado_em": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "dados_alterados_em": D["gerado_em"],
        "fontes": STATUS,
    }, ensure_ascii=False, indent=1))
    bad = [n for n, s in STATUS.items() if not s["ok"]]
    print("status:", json.dumps(STATUS, ensure_ascii=False))
    # falha total só se nada funcionou
    if len(bad) == len(STATUS):
        sys.exit(1)


if __name__ == "__main__":
    main()
