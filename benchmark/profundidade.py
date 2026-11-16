"""Benchmark de profundidade (D-48): menor caminho societário entre empresas, com limite de saltos crescente.

Uso: `uv run python -m benchmark.profundidade benchmark/saida/profundidade [--sem-recarga]`, com o Neo4j e o
PostgreSQL do perfil bench no ar. Reaproveita os 10 pares de maior grau do benchmark de referência (D-43).

Neo4j (shortestPath) x PostgreSQL em duas formas: CTE recursiva nível a nível (a forma idiomática, a mesma do
benchmark de referência) e busca em largura em PL/pgSQL com conjunto de visitados (a forma otimizada).
Todas devem dar a mesma menor distância. Só leitura no Neo4j.
"""

import json
import statistics
import sys
import time
from pathlib import Path

import psycopg
from dotenv import dotenv_values
from neo4j import READ_ACCESS, GraphDatabase

from benchmark import dados

SAIDA = Path(sys.argv[1])
SAIDA.mkdir(parents=True, exist_ok=True)
PROFUNDIDADES = [2, 4, 6, 8, 10, 12]
TIMEOUT = 300
RECARREGAR = "--sem-recarga" not in sys.argv

env = dotenv_values(".env")
drv = GraphDatabase.driver(
    env.get("NEO4J_URI", "bolt://localhost:7699"),
    auth=(env.get("NEO4J_USER", "neo4j"), env["NEO4J_PASSWORD"]),
)
pg = psycopg.connect("postgresql://sarc:sarc-bench-local@localhost:5440/sarc", autocommit=True)

if RECARREGAR:
    dados.exportar_csv(drv, SAIDA / "csv")
    pg.autocommit = False
    dados.carregar_postgres(pg, SAIDA / "csv")
    pg.commit()
    pg.autocommit = True

pg.execute("""
CREATE OR REPLACE FUNCTION bfs_menor(origem text, alvo text, maximo int) RETURNS int AS $$
DECLARE
  fronteira text[] := ARRAY[origem];
  nivel int := 0;
BEGIN
  IF origem = alvo THEN RETURN 0; END IF;
  CREATE TEMP TABLE IF NOT EXISTS visitado (no text PRIMARY KEY) ON COMMIT DROP;
  TRUNCATE visitado;
  INSERT INTO visitado VALUES (origem);
  WHILE nivel < maximo AND cardinality(fronteira) > 0 LOOP
    nivel := nivel + 1;
    WITH novos AS (
      INSERT INTO visitado
      SELECT DISTINCT r.destino_no FROM aresta_societaria r
      WHERE r.origem_no = ANY (fronteira)
      ON CONFLICT DO NOTHING
      RETURNING no
    ) SELECT array_agg(no) INTO fronteira FROM novos;
    IF alvo = ANY (fronteira) THEN RETURN nivel; END IF;
  END LOOP;
  RETURN NULL;
END $$ LANGUAGE plpgsql;
""")
pg.execute(f"SET statement_timeout = {TIMEOUT * 1000}")

CYPHER = """
MATCH (a:Empresa {cnpjBasico: $a}), (b:Empresa {cnpjBasico: $b})
OPTIONAL MATCH p = shortestPath((a)-[:SOCIO|SOCIO_PJ*..%d]-(b))
RETURN length(p) AS menor
"""
CTE = """
WITH RECURSIVE nivel (no, saltos) AS (
  SELECT 'E' || %%(a)s, 0
  UNION
  SELECT r.destino_no, n.saltos + 1
  FROM nivel n JOIN aresta_societaria r ON r.origem_no = n.no
  WHERE n.saltos < %d AND n.no <> 'E' || %%(b)s
)
SELECT min(saltos) AS menor FROM nivel WHERE no = 'E' || %%(b)s
"""


def medir(f, rapido=10.0, repeticoes=3):
    t0 = time.perf_counter()
    try:
        r = f()
    except Exception as e:  # noqa: BLE001
        return {"erro": type(e).__name__ + ": " + str(e)[:120], "tempos": [time.perf_counter() - t0]}
    tempos = [time.perf_counter() - t0]
    if tempos[0] < rapido:
        for _ in range(repeticoes):
            t0 = time.perf_counter()
            f()
            tempos.append(time.perf_counter() - t0)
        tempos = tempos[1:]  # a primeira execução aquece
    return {"menor": r, "tempos": tempos, "mediana": statistics.median(tempos)}


def neo(a, b, k):
    with drv.session(default_access_mode=READ_ACCESS) as s:
        return s.run(CYPHER % k, a=a, b=b, timeout=TIMEOUT).single()["menor"]


def cte(a, b, k):
    return pg.execute(CTE % k, {"a": a, "b": b}).fetchone()[0]


def bfs(a, b, k):
    with pg.transaction():
        return pg.execute("SELECT bfs_menor('E' || %s, 'E' || %s, %s)", (a, b, k)).fetchone()[0]


pares = json.load(open("benchmark/saida/referencia/resultados.json"))["pares"][:10]
saida = []
for i, p in enumerate(pares, 1):
    for k in PROFUNDIDADES:
        linha = {"par": i, "a": p["a"], "b": p["b"], "grau_a": p["grau_a"], "grau_b": p["grau_b"], "k": k}
        linha["neo4j"] = medir(lambda a=p["a"], b=p["b"], k=k: neo(a, b, k))
        linha["pg_bfs"] = medir(lambda a=p["a"], b=p["b"], k=k: bfs(a, b, k))
        cte_anterior = [x for x in saida if x["par"] == i and "erro" in x["pg_cte"]]
        linha["pg_cte"] = (
            {"erro": "pulado: estourou em profundidade menor", "tempos": []}
            if cte_anterior
            else medir(lambda a=p["a"], b=p["b"], k=k: cte(a, b, k), rapido=10.0, repeticoes=2)
        )
        menores = {v["menor"] for v in (linha["neo4j"], linha["pg_bfs"], linha["pg_cte"]) if "menor" in v}
        linha["iguais"] = len(menores) <= 1
        saida.append(linha)
        print(
            i,
            k,
            {
                v: (round(linha[v].get("mediana", -1) * 1000, 1), linha[v].get("menor"), linha[v].get("erro"))
                for v in ("neo4j", "pg_bfs", "pg_cte")
            },
            linha["iguais"],
            flush=True,
        )
        json.dump(saida, open(SAIDA / "profundidade.json", "w"), indent=1, default=str)
