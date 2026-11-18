"""EBITDA ajustado por m³ das distribuidoras, trimestral, das planilhas de RI.

Vibra: "Planilhas por Segmento" (aba de reconciliação do EBITDA, linha "MARGEM EBITDA AJUSTADA (R$/m³)"),
listadas pela API pública do provedor de RI (MZ). Lemos sempre o arquivo mais recente, que traz a série
histórica inteira.
"""
from __future__ import annotations

import datetime as dt
import io
import json
import re

MZ_META = "https://apicatalog.mziq.com/filemanager/company/{cid}/filter/categories/year/meta"
EMPRESAS = {
    "Vibra": {"cid": "d243bdaa-0468-4f64-8c09-ba0bcee9789b", "cat": "central-planilhas-segmento",
              "aba": r"reconcilia.*ebitda", "linha": r"margem ebitda ajustad.*r\$/m"},
}
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


def build(get, post) -> dict:
    out = {}
    for nome, cfg in EMPRESAS.items():
        meta = _latest(post, cfg["cid"], cfg["cat"])
        serie = _serie(get(meta["file_url"]).content, cfg["linha"], cfg["aba"])
        out[nome] = {"arquivo": meta["file_title"], "data_arquivo": meta["file_date"][:10], "serie": serie}
    return out
