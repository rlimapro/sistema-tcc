"""O banco recusa escrita feita por `ler()` (READ_ACCESS + execute_read), e o timeout vale."""

import os

import pytest
from neo4j.exceptions import ClientError

from sarc.grafo.conexao import abrir_driver_uri, escrever, ler
from tests.conftest import SENHA_TESTE_PADRAO, URI_TESTE_PADRAO

pytestmark = pytest.mark.integration

RECUSA_DE_ESCRITA = "Neo.ClientError.Statement.AccessMode"


def _recusada(driver, cypher: str, banco: str | None = None) -> None:
    with pytest.raises(ClientError) as erro:
        ler(driver, cypher, banco=banco)
    assert erro.value.code == RECUSA_DE_ESCRITA, erro.value


def _contar(driver) -> int:
    return ler(driver, "MATCH (n) RETURN count(n) AS n")[0]["n"]


def test_ler_devolve_linhas(neo4j_teste) -> None:
    assert ler(neo4j_teste, "RETURN 1 AS um") == [{"um": 1}]


def test_create_recusado_em_leitura(neo4j_teste) -> None:
    _recusada(neo4j_teste, "CREATE (n) RETURN n")
    assert _contar(neo4j_teste) == 0


def test_merge_set_delete_recusados_em_leitura(neo4j_teste) -> None:
    escrever(neo4j_teste, "CREATE (:Municipio {codigo: '999'})")
    _recusada(neo4j_teste, "MERGE (n:Intruso {x: 1})")
    _recusada(neo4j_teste, "MATCH (m:Municipio) SET m.nome = 'x'")
    _recusada(neo4j_teste, "MATCH (n) DETACH DELETE n")
    _recusada(neo4j_teste, "CREATE INDEX intruso IF NOT EXISTS FOR (n:Intruso) ON (n.x)")
    assert _contar(neo4j_teste) == 1
    assert ler(neo4j_teste, "MATCH (m:Municipio) RETURN m.nome AS nome") == [{"nome": None}]


@pytest.mark.parametrize("banco", [None, "system"])
def test_troca_de_senha_recusada_em_leitura(neo4j_teste, banco) -> None:
    _recusada(
        neo4j_teste,
        f"ALTER CURRENT USER SET PASSWORD FROM '{SENHA_TESTE_PADRAO}' TO 'senha-trocada-123'",
        banco=banco,
    )
    # A senha original continua valendo.
    uri = os.environ.get("NEO4J_TEST_URI", URI_TESTE_PADRAO)
    abrir_driver_uri(uri, "neo4j", os.environ.get("NEO4J_TEST_PASSWORD", SENHA_TESTE_PADRAO)).close()


def test_timeout_aborta_a_leitura(neo4j_teste) -> None:
    with pytest.raises(ClientError) as erro:
        ler(neo4j_teste, "UNWIND range(1, 300000000) AS x RETURN sum(x) AS s", timeout=0.5)
    assert "TransactionTimedOut" in erro.value.code
