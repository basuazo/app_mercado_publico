"""Volumen de CA y de adjuntos en prod (solo lectura) para estimar el costo de procesar TODOS los anexos.
Uso: .venv\\Scripts\\python scripts\\mercado\\volumen_anexos.py   (usa DATABASE_URL_PROD del .env)"""
from pathlib import Path

from dotenv import dotenv_values
from sqlalchemy import create_engine, text

env = dotenv_values(Path(__file__).resolve().parents[2] / ".env")
url = env["DATABASE_URL_PROD"].replace("postgresql://", "postgresql+psycopg://", 1)
Q = """
select region, count(*) as ca,
       count(*) filter (where coalesce(n_documentos,0) > 0) as con_adjuntos,
       sum(coalesce(n_documentos,0)) as adjuntos,
       count(*) filter (where n_documentos is null) as sin_detalle,
       count(distinct date(fecha_publicacion)) filter (where extract(isodow from fecha_publicacion) < 6) as dias_habiles
from compras_agiles
where fecha_publicacion >= now() - interval '30 days'
group by region order by ca desc
"""
QE = """
select lower(substring(d->>'nombre' from '\\.([A-Za-z0-9]{2,5})$')) as ext, count(*)
from compras_agiles, jsonb_array_elements(coalesce(documentos,'[]'::jsonb)) d
where fecha_publicacion >= now() - interval '30 days' group by 1 order by 2 desc limit 8
"""
with create_engine(url).connect() as c:
    print("Últimos 30 días, por región:")
    for r in c.execute(text(Q)):
        print(f"  región {r.region}: {r.ca} CA · {r.con_adjuntos} con adjuntos · {r.adjuntos} adjuntos · "
              f"{r.sin_detalle} sin detalle · {r.dias_habiles} días hábiles")
    print("Extensiones de los adjuntos:", ", ".join(f"{r[0]}={r[1]}" for r in c.execute(text(QE))))
