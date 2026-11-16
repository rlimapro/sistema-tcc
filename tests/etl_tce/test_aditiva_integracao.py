"""`ADITIVA` (aditivo -> original) no Neo4j de teste, a partir da fixture real (DECISOES D-35)."""

from __future__ import annotations

from collections import Counter
from pathlib import Path

import pytest

from sarc.etl_tce.carregador import carregar
from sarc.etl_tce.cli import extrair_e_transformar
from sarc.etl_tce.extrator import Extrator
from sarc.grafo.conexao import ler

pytestmark = pytest.mark.integration

FIXTURE = Path(__file__).parent.parent / "fixtures" / "tce" / "cache"

ARESTAS = """
MATCH (a:Contrato)-[:ADITIVA]->(o:Contrato)
RETURN a.id AS aditivo, a.classe AS classeAditivo, o.id AS original, o.classe AS classeOriginal
ORDER BY aditivo
"""


def _carregar(driver, tmp_path: Path, baldes: list[tuple[str, int]]) -> Counter:
    extrator = Extrator("http://nao-usado", tmp_path / "cache", cache_extra=[FIXTURE], so_cache=True)
    contagens: Counter[str] = Counter()
    for municipio, ano in baldes:
        dados = extrair_e_transformar(extrator, municipio, ano, ano, contagens)
        carregar(driver, contagens=contagens, **dados)
    assert extrator.relatorio.faltando == []
    return contagens


def test_aditivos_ligados_ao_original_e_nunca_a_partir_de_original(neo4j_teste, tmp_path):
    contagens = _carregar(neo4j_teste, tmp_path, [("087", 2024), ("074", 2019)])
    arestas = ler(neo4j_teste, ARESTAS)
    assert len(arestas) == 4 + 3
    assert {a["classeOriginal"] for a in arestas} == {"original"}
    assert all(a["classeAditivo"] != "original" for a in arestas)
    iguatu = [a for a in arestas if a["aditivo"].startswith("074|")]
    assert {a["original"] for a in iguatu} == {"074|2019.01.18.07|2019-01-18"}
    # Iguatu: o original que aponta para si mesmo não ganha idOriginal nem aresta (nem laço).
    assert (
        ler(
            neo4j_teste,
            "MATCH (c:Contrato {classe: 'original'}) "
            "WHERE c.idOriginal IS NOT NULL OR (c)-[:ADITIVA]->() RETURN count(c) AS n",
        )[0]["n"]
        == 0
    )
    assert contagens["aditivo_ligado"] == 7 and contagens["aditivo_orfao"] == 0
    vigencia = ler(
        neo4j_teste,
        "MATCH (c:Contrato {id: '087|20240412000201|2024-12-30#202404120002|2024-04-12'}) "
        "RETURN toString(c.dataFimVigencia) AS fim, c.tipoObjeto AS tipo",
    )
    assert vigencia == [{"fim": "2025-04-12", "tipo": "O"}]


def test_aditivo_religado_quando_o_original_chega_em_carga_posterior(neo4j_teste, tmp_path):
    # O aditivo de 2021 aponta para um original de 2018: carregado antes dele, fica órfão.
    primeira = _carregar(neo4j_teste, tmp_path, [("087", 2021)])
    assert primeira["aditivo_orfao"] == 1 and primeira["aditivo_ligado"] == 0
    assert ler(neo4j_teste, ARESTAS) == []

    segunda = _carregar(neo4j_teste, tmp_path, [("087", 2018)])
    assert segunda["aditivo_orfao"] == 0 and segunda["aditivo_ligado"] == 1
    assert [(a["aditivo"], a["original"]) for a in ler(neo4j_teste, ARESTAS)] == [
        ("087|2018008406|2021-01-28#20180084|2018-08-01", "087|20180084|2018-08-01")
    ]


def test_aditiva_idempotente(neo4j_teste, tmp_path):
    baldes = [("087", 2021), ("087", 2024), ("074", 2019), ("087", 2018)]
    _carregar(neo4j_teste, tmp_path, baldes)
    primeira = ler(neo4j_teste, ARESTAS)
    contagens = _carregar(neo4j_teste, tmp_path, baldes)
    assert ler(neo4j_teste, ARESTAS) == primeira and len(primeira) == 8
    # Situação do grafo, não o que a carga criou: a 2ª execução vê os mesmos aditivos ligados.
    assert contagens["aditivo_orfao"] == 0
