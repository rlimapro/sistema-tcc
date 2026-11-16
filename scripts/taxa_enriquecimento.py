"""Taxa de enriquecimento dos estabelecimentos com dados da Receita Federal, em JSON.

Uso:
    uv run python scripts/taxa_enriquecimento.py --env-file .env

Somente leitura. Um estabelecimento conta como enriquecido quando tem `situacaoCadastral`, campo que só
o ETL da RFB grava (`sarc/etl_rfb/carregador.py`, CYPHER_ESTABELECIMENTO); o ETL do TCE-CE cria o nó só
com `cnpj`, `cnpjBasico` e `razaoSocialTce`. Os não enriquecidos são CNPJs do TCE-CE ausentes do
snapshot da RFB em uso. A taxa sai no total e por papel do estabelecimento no grafo (um mesmo
estabelecimento pode ter mais de um papel): licitante (PARTICIPOU_DE), contratado (CONTRATADO_EM) e
credor da execução orçamentária (CREDOR_NA_FILA).
"""

from __future__ import annotations

import argparse
import json
from typing import LiteralString

from sarc.config import carregar
from sarc.grafo.conexao import abrir_driver, ler

CONSULTA: LiteralString = """
MATCH (e:Estabelecimento)
RETURN count(e) AS total,
       count(e.situacaoCadastral) AS comRfb,
       count(CASE WHEN EXISTS { (e)-[:PARTICIPOU_DE]->() } THEN 1 END) AS licitante,
       count(CASE WHEN EXISTS { (e)-[:PARTICIPOU_DE]->() } AND e.situacaoCadastral IS NOT NULL THEN 1 END)
         AS licitanteComRfb,
       count(CASE WHEN EXISTS { (e)-[:CONTRATADO_EM]->() } THEN 1 END) AS contratado,
       count(CASE WHEN EXISTS { (e)-[:CONTRATADO_EM]->() } AND e.situacaoCadastral IS NOT NULL THEN 1 END)
         AS contratadoComRfb,
       count(CASE WHEN EXISTS { (e)-[:CREDOR_NA_FILA]->() } THEN 1 END) AS credor,
       count(CASE WHEN EXISTS { (e)-[:CREDOR_NA_FILA]->() } AND e.situacaoCadastral IS NOT NULL THEN 1 END)
         AS credorComRfb
"""

CONSULTA_SNAPSHOT: LiteralString = """
MATCH (c:Carga {etapa: 'etl-rfb'})
RETURN c.snapshotRfb AS snapshotRfb, c.status AS status, c.fim AS fim ORDER BY c.fim DESC LIMIT 1
"""


def _linha(total: int, com: int) -> dict:
    return {
        "total": total,
        "comDadosRfb": com,
        "semDadosRfb": total - com,
        "percentualEnriquecido": round(100 * com / total, 2) if total else None,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--env-file", required=True)
    args = parser.parse_args()
    with abrir_driver(carregar(args.env_file)) as driver:
        r = dict(ler(driver, CONSULTA, {})[0])
        carga = ler(driver, CONSULTA_SNAPSHOT, {})
    saida = {
        "criterio": "estabelecimento com situacaoCadastral (campo gravado só pelo ETL da RFB)",
        "cargaRfb": {k: str(v) for k, v in dict(carga[0]).items()} if carga else None,
        "todos": _linha(r["total"], r["comRfb"]),
        "porPapel": {
            "licitante": _linha(r["licitante"], r["licitanteComRfb"]),
            "contratado": _linha(r["contratado"], r["contratadoComRfb"]),
            "credor": _linha(r["credor"], r["credorComRfb"]),
        },
    }
    print(json.dumps(saida, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
