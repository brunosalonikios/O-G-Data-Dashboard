"""Carros elétricos e híbridos no Brasil: emplacamentos mensais por combustível (Anfavea).

Fonte: "Carta da Anfavea" (PDF mensal), tabela "Emplacamento de automóveis e comerciais leves novos
por combustível - nacionais e importados", que traz o ano corrente e o anterior, mês a mês. Lemos a
carta mais recente e algumas antigas para montar o histórico; cada carta lida fica em cache.
"""
from __future__ import annotations

import io
import re

CARTA = "https://www.anfavea.com.br/cartas/carta{n}.pdf"
PAGINA = "https://anfavea.com.br/site/conteudos/carta-da-anfavea/"
MESES = ["JAN", "FEV", "MAR", "ABR", "MAI", "JUN", "JUL", "AGO", "SET", "OUT", "NOV", "DEZ"]
LINHAS = [  # (regex do rótulo, chave)
    (r"\bH[íi]brido\s*/\s*El[ée]tric", "hib_ele"), (r"\bEl[ée]tric", "eletrico"), (r"\bH[íi]brido\s*Plug", "phev"), (r"\bH[íi]brido", "hibrido"),
    (r"\bFlex", "flex"), (r"\bGasolina", "gasolina"), (r"\bDiesel", "diesel"), (r"\bEtanol", "etanol"),
]


def _num(t: str):
    t = t.strip()
    if not re.fullmatch(r"[\d.]+", t):
        return None
    return int(t.replace(".", ""))


def ler_carta(pdf: bytes) -> dict:
    """{"2026-03": {"eletrico": 14224, ...}} a partir da tabela de leves por combustível."""
    import pdfplumber
    with pdfplumber.open(io.BytesIO(pdf)) as p:
        for pg in p.pages:
            t = pg.extract_text() or ""
            if not re.search(r"(comerciais leves|comerciales livianos).{0,20}por\s*combust", t, re.I):
                continue
            words = pg.extract_words(keep_blank_chars=False, use_text_flow=False)
            tit = min((w for w in words if w["text"].lower().startswith("combust")), key=lambda w: w["top"])
            # cabeçalho de meses da primeira tabela (Unidades), logo abaixo do título
            heads = {}
            for w in sorted(words, key=lambda w: w["top"]):
                if w["top"] > tit["top"] and w["text"] in MESES and w["text"] not in heads:
                    heads[w["text"]] = w
            if len(heads) < 12:
                continue
            y0 = max(h["bottom"] for h in heads.values())
            part = next((w for w in words if w["text"].startswith("Participa") and w["top"] > y0), None)
            y1 = part["top"] if part else pg.height
            xs = {m: (h["x0"] + h["x1"]) / 2 for m, h in heads.items()}
            xmin = min(h["x0"] for h in heads.values()) - 15
            passo = xs["DEZ"] - xs["NOV"]
            xmax = xs["DEZ"] + passo / 2
            dentro = [w for w in words if y0 < w["top"] < y1]
            # rótulos: palavras à esquerda da tabela, agrupadas por linha
            rot_lin = {}
            for w in sorted((w for w in dentro if w["x1"] < xmin and not re.fullmatch(r"[\d.]+", w["text"])), key=lambda w: w["top"] + w["bottom"]):
                c = (w["top"] + w["bottom"]) / 2
                k = next((k for k in rot_lin if abs(k - c) <= 3), None)
                rot_lin.setdefault(c if k is None else k, []).append(w)
            nums = [w for w in dentro if w["x0"] >= xmin and _num(w["text"]) is not None]
            blocos, atual = [], {}
            for yc in sorted(rot_lin):
                rot = " ".join(w["text"] for w in sorted(rot_lin[yc], key=lambda w: w["x0"]))
                chave = next((c for rx, c in LINHAS if re.search(rx, rot.strip(), re.I)), None)
                if not chave:
                    continue
                if chave in atual:                 # rótulo repetido: começou o bloco do ano seguinte
                    blocos.append(atual)
                    atual = {}
                vals = {}
                for w in nums:
                    c = (w["x0"] + w["x1"]) / 2
                    if abs((w["top"] + w["bottom"]) / 2 - yc) > 4 or c > xmax:
                        continue
                    m = min(xs, key=lambda mm: abs(xs[mm] - c))
                    vals[m] = _num(w["text"])
                atual[chave] = vals
            if atual:
                blocos.append(atual)
            anos = [int(a) for a in re.findall(r"\b(20\d\d)\b", t)]
            ano = max(a for a in anos if a < 2100)
            if len(blocos) < 2:
                continue
            blocos = blocos[:2]                    # tabela de unidades: ano anterior e ano corrente
            out = {}
            for b, y in zip(blocos, (ano - 1, ano)):
                for chave, vals in b.items():
                    for m, v in vals.items():
                        out.setdefault(f"{y}-{MESES.index(m) + 1:02d}", {})[chave] = v
            return out
    raise RuntimeError("tabela de leves por combustível não encontrada")


def numero_atual(get) -> int:
    html = get(PAGINA).text
    return max(int(n) for n in re.findall(r"cartas/carta(\d+)\.pdf", html))


def build(get, cache: dict, anos_hist: int = 7) -> dict:
    """cache: {"cartas": {"486": {...meses...}}}. Lê a carta atual (sempre) e uma por ano para trás."""
    n = numero_atual(get)
    cartas = cache.setdefault("cartas", {})
    alvo = [n] + [n - 12 * k for k in range(1, anos_hist + 1)]   # cada carta traz o ano anterior inteiro
    for c in alvo:
        if str(c) in cartas and c != n:
            continue
        try:
            cartas[str(c)] = ler_carta(get(CARTA.format(n=c)).content)
        except Exception as e:  # noqa: BLE001
            print(f"Anfavea carta {c} falhou: {str(e)[:100]}")
    meses = {}
    for c in sorted(cartas, key=int):          # carta mais nova prevalece (revisões)
        for m, v in cartas[c].items():
            if sum(v.values()) > 0:
                meses[m] = v
    serie = []
    for m in sorted(meses):
        v = meses[m]
        tot = sum(v.values())
        serie.append({"mes": m, **v, "total": tot})
    return {"carta": n, "serie": serie}
