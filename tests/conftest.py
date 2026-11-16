"""Fixtures comuns.

Banco de teste: `neo4j_teste` conecta em `NEO4J_TEST_URI` (padrão bolt://localhost:7700,
o serviço `neo4j-teste` do docker-compose, perfil `teste`) e NUNCA no banco do `.env`.
Se não conectar, os testes de integração são pulados. Se o banco já tiver nós no
início da sessão, a fixture se recusa a rodar (CRITICA F-21): assim um engano de
porta não apaga a carga real. Entre os testes, o banco de teste é esvaziado.

Uso nos testes de integração:

    pytestmark = pytest.mark.integration

    def test_algo(neo4j_teste):  # banco vazio, com o esquema aplicado
        ...
"""

from __future__ import annotations

import os
from collections.abc import Iterator

import pytest
from neo4j import Driver

from sarc.grafo.conexao import abrir_driver_uri, escrever, ler
from sarc.grafo.esquema import aplicar_esquema

URI_TESTE_PADRAO = "bolt://localhost:7700"
SENHA_TESTE_PADRAO = "sarc-teste-local"


@pytest.fixture(autouse=True)
def _restaura_ambiente() -> Iterator[None]:
    """`load_dotenv` (CLI, `carregar`) escreve em `os.environ`; nada vaza de um teste para outro."""
    original = dict(os.environ)
    yield
    os.environ.clear()
    os.environ.update(original)


def _contar_nos(driver: Driver) -> int:
    return ler(driver, "MATCH (n) RETURN count(n) AS n")[0]["n"]


@pytest.fixture(scope="session")
def _driver_teste() -> Iterator[Driver]:
    uri = os.environ.get("NEO4J_TEST_URI", URI_TESTE_PADRAO)
    usuario = os.environ.get("NEO4J_TEST_USER", "neo4j")
    senha = os.environ.get("NEO4J_TEST_PASSWORD", SENHA_TESTE_PADRAO)
    try:
        driver = abrir_driver_uri(uri, usuario, senha)
    except Exception as erro:  # noqa: BLE001 - sem banco de teste, pula
        pytest.skip(
            f"Neo4j de teste indisponível em {uri} ({type(erro).__name__}). "
            "Suba com: docker compose --profile teste up -d neo4j-teste"
        )
    try:
        nos = _contar_nos(driver)
        if nos:
            pytest.exit(
                f"o banco de teste em {uri} tem {nos} nós no início da sessão; os testes só rodam num banco "
                "vazio, para nunca apagar uma carga real. Confira NEO4J_TEST_URI ou recrie o container "
                "(docker compose --profile teste up -d --force-recreate neo4j-teste).",
                returncode=3,
            )
        aplicar_esquema(driver)
        yield driver
    finally:
        driver.close()


@pytest.fixture
def neo4j_teste(_driver_teste: Driver) -> Iterator[Driver]:
    """Driver do banco de teste, vazio e com o esquema aplicado; esvaziado de novo ao final."""
    yield _driver_teste
    escrever(_driver_teste, "MATCH (n) DETACH DELETE n")
