"""Argentina poço a poço: Vaca Muerta, shale vs convencional, operadores, poços novos e produtividade.

Fonte: Secretaria de Energia da Argentina, produção mensal por poço (Capítulo IV / SESCO),
redistribuída em parquet pelo projeto PetroDB (CC BY 4.0) no Hugging Face, porque o portal
oficial não responde fora da Argentina. Só baixamos de novo quando o repositório muda.
"""
from __future__ import annotations

import calendar
import json
import tempfile
from pathlib import Path

REPO = "https://huggingface.co/datasets/oskrgab/petrodb"
BASE = f"{REPO}/resolve/main/argentina"
API = "https://huggingface.co/api/datasets/oskrgab/petrodb"
DESDE = 2014
BBL = 6.28981

GRUPOS = [  # (trecho do nome normalizado, grupo)
    ("YPF", "YPF"), ("VISTA", "Vista"), ("PAMPA", "Pampa"), ("PAN AMERICAN", "Pan American"),
    ("PLUSPETROL", "Pluspetrol"), ("SHELL", "Shell"), ("CHEVRON", "Chevron"), ("TECPETROL", "Tecpetrol"),
    ("TOTAL AUSTRAL", "TotalEnergies"), ("TOTALENERGIES", "TotalEnergies"), ("EXXON", "ExxonMobil"),
    ("EQUINOR", "Equinor"), ("PHOENIX", "Phoenix"), ("CGC", "CGC"), ("COMPAÑIA GENERAL DE COMBUSTIBLES", "CGC"),
    ("CAPEX", "Capex"), ("PETRONAS", "Petronas"), ("TOTAL", "TotalEnergies"), ("HARBOUR", "Harbour"),
    ("PETROLERA EL TREBOL", "El Trébol"), ("WINTERSHALL", "Wintershall"), ("MADALENA", "Madalena"),
    ("GEOPARK", "GeoPark"), ("CRUSADER", "Crusader"), ("ROCH", "Roch"), ("CAPSA", "CAPSA"),
    ("COMPAÑÍAS ASOCIADAS PETROLERAS", "CAPSA"), ("SINOPEC", "Sinopec"), ("ENAP", "ENAP"),
]


def grupo(nome: str) -> str:
    n = (nome or "").upper()
    for k, v in GRUPOS:
        if k in n:
            return v
    return "Outras"


def versao(get) -> str:
    return json.loads(get(API).content)["sha"]


def build(get) -> dict:
    import duckdb

    tmp = Path(tempfile.mkdtemp())
    manifest = json.loads(get(f"{BASE}/monthly_production/_files.json").content)
    anos = [p for p in manifest if int(p.split("=")[1][:4]) >= DESDE]
    for p in anos:
        (tmp / f"p{p.split('=')[1][:4]}.parquet").write_bytes(get(f"{BASE}/monthly_production/{p}").content)
    (tmp / "wells.parquet").write_bytes(get(f"{BASE}/wells.parquet").content)
    (tmp / "woh.parquet").write_bytes(get(f"{BASE}/well_operator_history.parquet").content)

    con = duckdb.connect()
    con.sql("set enable_progress_bar = false")
    con.create_function("grupo", grupo, ["VARCHAR"], "VARCHAR")
    con.sql(f"""
        create table m as
        select m.idpozo, m.fecha, coalesce(m.prod_pet,0) pet, coalesce(m.prod_gas,0) gas,
               w.formprod = 'VMUT' as vm, w.tipo_recurso, w.clasificacion,
               grupo(coalesce(o.empresa, w.empresa)) as op
        from read_parquet('{tmp}/p*.parquet') m
        join read_parquet('{tmp}/wells.parquet') w using (idpozo)
        left join read_parquet('{tmp}/woh.parquet') o
          on o.idpozo = m.idpozo and m.fecha between o.valid_from and o.valid_to
    """)

    def dias(mes):
        y, mm = map(int, mes.split("-"))
        return calendar.monthrange(y, mm)[1]

    def serie(sql, chave):
        out = {}
        for mes, k, pet, gas in con.sql(sql).fetchall():
            mes = mes.strftime("%Y-%m")
            d = dias(mes)
            r = out.setdefault(mes, {"mes": mes, "oil": {}, "gas": {}})
            r["oil"][k] = round(pet / d * BBL / 1000, 1)     # mil bbl/d (fonte: m³/mês)
            r["gas"][k] = round(gas / d / 1000, 2)           # mi m³/d (fonte: mil m³/mês)
        return [out[k] for k in sorted(out)]

    # 1) Vaca Muerta x resto do país
    vm_total = serie("""select fecha, case when vm then 'Vaca Muerta' else 'Demais formações' end k,
                        sum(pet), sum(gas) from m group by all order by 1""", "k")
    # 2) Vaca Muerta por operador (top 8 no último mês; resto em Outras)
    ult = con.sql("select max(fecha) from m").fetchall()[0][0]
    top_oil = [r[0] for r in con.sql(f"""select op, sum(pet) from m where vm and fecha = '{ult}' and op <> 'Outras'
                                         group by 1 order by 2 desc limit 8""").fetchall()]
    top_gas = [r[0] for r in con.sql(f"""select op, sum(gas) from m where vm and fecha = '{ult}' and op <> 'Outras'
                                         group by 1 order by 2 desc limit 8""").fetchall()]
    tops = sorted(set(top_oil) | set(top_gas))
    lista = ",".join("'" + t.replace("'", "''") + "'" for t in tops)
    vm_op = serie(f"""select fecha, case when op in ({lista}) then op else 'Outras' end k, sum(pet), sum(gas)
                      from m where vm group by all order by 1""", "k")
    # 3) ranking do país por operador no último mês e um ano antes
    ano_antes = ult.replace(year=ult.year - 1)
    rk = {}
    for f, op, pet, gas, pvm in con.sql(f"""select fecha, op, sum(pet), sum(gas), sum(case when vm then pet else 0 end)
                                       from m where fecha in ('{ult}', '{ano_antes}') group by all""").fetchall():
        d = dias(f.strftime("%Y-%m"))
        r = rk.setdefault(op, {"empresa": op})
        sfx = "" if f == ult else "_aa"
        r["oil" + sfx] = round(pet / d * BBL / 1000, 1)
        r["gas" + sfx] = round(gas / d / 1000, 2)
        if f == ult:
            r["oil_vm"] = round(pvm / d * BBL / 1000, 1)
    ranking = sorted([r for r in rk.values() if r["empresa"] != "Outras"], key=lambda r: -r.get("oil", 0))[:15]
    # 4) poços novos em Vaca Muerta (mês da primeira produção), petróleo x gás
    novos = {}
    for mes, cls, n in con.sql("""
            with f as (select idpozo, min(fecha) f0, any_value(clasificacion) cls from m
                       where vm and (pet > 0 or gas > 0) group by 1)
            select f0, case when cls ilike 'gas%' then 'gas' else 'oil' end, count(*) from f
            where year(f0) >= 2015 group by all order by 1""").fetchall():
        mes = mes.strftime("%Y-%m")
        novos.setdefault(mes, {"mes": mes, "oil": 0, "gas": 0})[cls] = n
    # primeiro mês do arquivo: poços antigos aparecem como "novos"; descarta
    novos = [novos[k] for k in sorted(novos) if k > f"{DESDE}-01"]
    # 5) produtividade: média de petróleo nos 6 primeiros meses, por safra, poços de óleo em VM
    prod = []
    for ano, op, n, media in con.sql(f"""
            with f as (select idpozo, min(fecha) f0 from m where vm and pet > 0 group by 1),
            s as (select m.idpozo, year(f.f0) ano, arg_max(m.op, m.fecha) op, sum(m.pet) pet6
                  from m join f using (idpozo)
                  where m.fecha < f.f0 + interval 6 month
                    and f.f0 <= date '{ult}' - interval 5 month group by 1, 2)
            select ano, op, count(*), avg(pet6 / 182.5 * 6.28981) from s
            where ano >= 2015 group by grouping sets ((ano, op), (ano)) order by 1""").fetchall():
        prod.append({"ano": int(ano), "empresa": op or "Todos", "pocos": int(n), "bbl_d": round(media, 0)})
    return {
        "fonte_ultimo_mes": ult.strftime("%Y-%m"),
        "vm_total": vm_total, "vm_operadores": vm_op, "ranking": ranking,
        "pocos_novos_vm": novos, "produtividade_vm": prod,
    }
