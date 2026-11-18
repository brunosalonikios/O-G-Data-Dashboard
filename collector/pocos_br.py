"""Acompanhamento poço a poço no Brasil: quais poços produzem, quais pararam, voltaram ou são novos.

Fonte: ANP, "produção por zona" (arquivo mensal com cada poço/zona produtora). Somamos as zonas de
cada poço. Guardamos, por mês, só os poços com produção (json.gz em data/cache/pocos_br/), e
recalculamos tudo a cada rodada a partir desses arquivos. Janela: os últimos MESES meses.
"""
from __future__ import annotations

import calendar
import csv
import gzip
import io
import json
from collections import defaultdict
from pathlib import Path

MESES = 37            # 36 meses de série + 1 para saber quem é novo no primeiro mês mostrado
BBL = 6.28981
MIN_PICO = 300        # bbl/d de óleo equivalente: corte para a lista de poços parados


def _num(s) -> float:
    s = (s or "").strip().replace(".", "").replace(",", ".")
    try:
        return float(s)
    except ValueError:
        return 0.0


def _mes(get, url: str, campo_nome) -> dict:
    txt = get(url).content.decode("utf-8-sig", errors="replace")
    pocos: dict[str, list] = {}
    for r in csv.DictReader(io.StringIO(txt)):
        nome = (r.get("Nome poço ANP") or "").strip()
        if not nome:
            continue
        oil = _num(r.get("Petróleo (m³/d)")) * BBL
        gas = _num(r.get("Gás total (Mil m³/d)"))
        p = pocos.setdefault(nome, [0.0, 0.0, campo_nome(r.get("Campo", "")), r.get("Bacia") or "",
                                    (r.get("Ambiente") or "").upper()])
        p[0] += oil
        p[1] += gas
    # só poços com produção; óleo em bbl/d, gás em mil m³/d
    return {k: [round(v[0], 1), round(v[1], 2), v[2], v[3], v[4]] for k, v in pocos.items() if v[0] > 0 or v[1] > 0}


def build(get, by_month: dict, cache_dir: Path, operador) -> dict:
    cache_dir.mkdir(parents=True, exist_ok=True)
    meses = sorted(by_month)[-MESES:]
    from collect import _field_name  # noqa: PLC0415  (mesma normalização de nome de campo)
    dados = {}
    for i, m in enumerate(meses):
        p = cache_dir / f"{m}.json.gz"
        recente = i >= len(meses) - 2          # os 2 últimos meses podem ser revisados pela ANP
        if p.exists() and not recente:
            dados[m] = json.loads(gzip.decompress(p.read_bytes()))
            continue
        try:
            d = _mes(get, by_month[m], _field_name)
            if not d:
                raise RuntimeError("mês vazio")
            novo = gzip.compress(json.dumps(d, ensure_ascii=False, separators=(",", ":")).encode(), mtime=0)
            if not p.exists() or p.read_bytes() != novo:
                p.write_bytes(novo)
            dados[m] = d
        except Exception as e:  # noqa: BLE001
            if p.exists():
                dados[m] = json.loads(gzip.decompress(p.read_bytes()))
            else:
                print(f"poços {m} falhou: {e}")
    for f in cache_dir.glob("*.json.gz"):           # fora da janela
        if f.name[:7] not in meses:
            f.unlink()
    meses = [m for m in meses if m in dados]
    # descarta mês final claramente incompleto
    if len(meses) > 2 and len(dados[meses[-1]]) < 0.7 * len(dados[meses[-2]]):
        meses = meses[:-1]
    ult, ant = meses[-1], meses[-2]
    boe = lambda v: (v[0] + v[1] * BBL) if v else 0.0   # noqa: E731  (convenção ANP: 1 mil m³ de gás = 1 m³ de óleo)

    meta = {}
    for m in meses:
        for k, v in dados[m].items():
            meta[k] = v[2:]
    op = {k: operador(v[0]) for k, v in meta.items()}

    # 1) contagem mensal
    contagem = []
    vistos = set(dados[meses[0]])
    for a, m in zip(meses, meses[1:]):
        cur, prev = dados[m], dados[a]
        novos = [k for k in cur if k not in vistos]
        contagem.append({
            "mes": m, "produzindo": len(cur),
            "mar": sum(1 for v in cur.values() if v[4] == "MAR"),
            "terra": sum(1 for v in cur.values() if v[4] != "MAR"),
            "novos": len(novos),
            "pararam": sum(1 for k in prev if k not in cur),
            "voltaram": sum(1 for k in cur if k not in prev and k in vistos),
        })
        vistos |= set(cur)

    def linha(k, extra):
        c, b, amb = meta[k]
        return {"poco": k, "campo": c.replace("_", " ").title(), "bacia": b, "amb": "Mar" if amb == "MAR" else "Terra",
                "operador": op[k], **extra}

    serie_m = meses[-13:]
    serie = lambda k: [round(dados[m][k][0]) if k in dados[m] else 0 for m in serie_m]   # noqa: E731

    # 2) poços parados: produziam forte na janela e estão sem produção no último mês
    parados = []
    for k in meta:
        if k in dados[ult]:
            continue
        hist = [(m, dados[m][k]) for m in meses if k in dados[m]]
        pico_m, pico = max(hist, key=lambda t: boe(t[1]))
        if boe(pico) < MIN_PICO:
            continue
        ultimo_m, ultimo = hist[-1]
        parado_desde = meses[meses.index(ultimo_m) + 1]
        parados.append(linha(k, {"parado_desde": parado_desde, "ultima_oil": round(ultimo[0]), "ultima_gas": ultimo[1],
                                 "pico_boe": round(boe(pico)), "novo": parado_desde == ult, "serie": serie(k)}))
    parados.sort(key=lambda r: (-r["novo"], -boe([r["ultima_oil"], r["ultima_gas"]])))
    # 3) voltaram e novos no último mês
    antes = set().union(*(set(dados[m]) for m in meses[:-1]))
    voltaram = [linha(k, {"oil": round(v[0]), "gas": v[1], "parado_desde": next(
                    (meses[i + 1] for i in range(len(meses) - 2, -1, -1) if k in dados[meses[i]]), None)})
                for k, v in dados[ult].items() if k not in dados[ant] and k in antes]
    novos = [linha(k, {"oil": round(v[0]), "gas": v[1]}) for k, v in dados[ult].items() if k not in antes]
    for lst in (voltaram, novos):
        lst.sort(key=lambda r: -boe([r["oil"], r["gas"]]))
    # 4) maiores poços e maiores variações no mês
    top = sorted(dados[ult].items(), key=lambda t: -t[1][0])[:25]
    maiores = [linha(k, {"oil": round(v[0]), "gas": v[1], "oil_ant": round(dados[ant].get(k, [0])[0]),
                         "serie": serie(k)}) for k, v in top]
    var = []
    for k, v in dados[ult].items():
        if k in dados[ant]:
            d = v[0] - dados[ant][k][0]
            var.append((d, k, v))
    var.sort()
    quedas = [linha(k, {"oil": round(v[0]), "oil_ant": round(dados[ant][k][0]), "delta": round(d)}) for d, k, v in var[:15] if d < 0]
    altas = [linha(k, {"oil": round(v[0]), "oil_ant": round(dados[ant][k][0]), "delta": round(d)}) for d, k, v in var[::-1][:15] if d > 0]
    # 5) por operador no último mês
    por_op = defaultdict(lambda: {"produzindo": 0, "pararam": 0, "voltaram": 0, "novos": 0})
    for k in dados[ult]:
        por_op[op[k]]["produzindo"] += 1
    for k in dados[ant]:
        if k not in dados[ult]:
            por_op[op[k]]["pararam"] += 1
    for r in voltaram:
        por_op[r["operador"]]["voltaram"] += 1
    for r in novos:
        por_op[r["operador"]]["novos"] += 1
    ops = sorted(({"operador": k, **v} for k, v in por_op.items()), key=lambda r: -r["produzindo"])
    return {
        "mes": ult, "mes_anterior": ant, "serie_meses": serie_m,
        "contagem": contagem[12:],   # nos primeiros meses da janela "novo" ainda não tem histórico suficiente
        "parados": parados[:40], "voltaram": voltaram[:20], "novos": novos[:20],
        "maiores": maiores, "quedas": quedas, "altas": altas, "por_operador": ops[:15],
        "dias_mes": calendar.monthrange(int(ult[:4]), int(ult[5:]))[1],
    }
