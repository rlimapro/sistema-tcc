"""Carga da fixture real no Neo4j de teste: idempotência e o que o grafo deve (e não deve) conter."""

from __future__ import annotations

import os
from collections import Counter
from pathlib import Path

import pytest
from click.testing import CliRunner

from sarc.cli import grupo
from sarc.etl_tce.carregador import carregar
from sarc.etl_tce.cli import extrair_e_transformar
from sarc.etl_tce.extrator import Extrator
from sarc.grafo.conexao import ler

pytestmark = pytest.mark.integration

FIXTURE = Path(__file__).parent.parent / "fixtures" / "tce" / "cache"

CONTAGEM = """
CALL () { MATCH (n) RETURN count(n) AS nos }
CALL () { MATCH ()-[r]->() RETURN count(r) AS arestas }
CALL () { MATCH (n) UNWIND labels(n) AS rotulo RETURN collect(rotulo) AS rotulos }
CALL () { MATCH ()-[r]->() RETURN collect(type(r)) AS tipos }
RETURN nos, arestas, rotulos, tipos
"""


def _contar(driver) -> tuple[int, int, Counter, Counter]:
    linha = ler(driver, CONTAGEM)[0]
    return linha["nos"], linha["arestas"], Counter(linha["rotulos"]), Counter(linha["tipos"])


def _carregar_fixture(driver, tmp_path: Path) -> Counter:
    extrator = Extrator("http://nao-usado", tmp_path / "cache", cache_extra=[FIXTURE], so_cache=True)
    contagens: Counter[str] = Counter()
    for municipio, anos in (("087", (2021, 2024)), ("170", (2009,))):
        for ano in anos:
            dados = extrair_e_transformar(extrator, municipio, ano, ano, contagens)
            carregar(driver, contagens=contagens, **dados)
    assert extrator.relatorio.faltando == []
    return contagens


def test_carga_duas_vezes_e_idempotente(neo4j_teste, tmp_path):
    _carregar_fixture(neo4j_teste, tmp_path)
    primeira = _contar(neo4j_teste)
    _carregar_fixture(neo4j_teste, tmp_path)
    assert _contar(neo4j_teste) == primeira
    nos, arestas, rotulos, tipos = primeira
    assert rotulos["Municipio"] == 2 and rotulos["Licitacao"] == 6
    assert tipos["PARTICIPOU_DE"] > 0 and tipos["DECLAROU_CONTATO"] > 0
    assert tipos["ORIGINADO_DE"] > 0 and tipos["CONTRATADO_EM"] > 0


def test_conteudo_do_grafo(neo4j_teste, tmp_path):
    contagens = _carregar_fixture(neo4j_teste, tmp_path)

    # Nenhuma pessoa física e nenhum CNPJ fora de 14 dígitos.
    ruins = ler(
        neo4j_teste,
        "MATCH (e:Estabelecimento) WHERE size(e.cnpj) <> 14 OR e.razaoSocialTce CONTAINS 'ANONIMIZADA' "
        "RETURN count(e) AS n",
    )
    assert ruins[0]["n"] == 0
    assert contagens["licitante_pf_descartado"] == 1 and contagens["contratado_pf_descartado"] == 1

    # Data do certame no nó e na aresta; colisão marcada; Trairi é licitação de fato.
    lic = {
        r["id"]: r
        for r in ler(
            neo4j_teste,
            "MATCH (l:Licitacao)-[:DO_MUNICIPIO]->(m:Municipio) "
            "RETURN l.id AS id, l.chaveColidida AS colidida, "
            "l.licitacaoDeFato AS deFato, toString(l.dataRealizacao) AS data, m.nome AS municipio",
        )
    }
    assert lic["087|0052021PC"]["colidida"] is True
    assert lic["170|200901202"]["deFato"] is True and lic["170|200901202"]["municipio"] == "TRAIRI"
    datas = ler(
        neo4j_teste,
        "MATCH (:Estabelecimento)-[p:PARTICIPOU_DE]->(l:Licitacao) "
        "RETURN count(*) AS total, "
        "sum(CASE WHEN p.dataRealizacao = l.dataRealizacao THEN 1 ELSE 0 END) AS iguais",
    )[0]
    assert datas["total"] == datas["iguais"] > 0

    # O par do acórdão de Itatira declara o mesmo endereço no certame, com a data do certame.
    par = ler(
        neo4j_teste,
        "MATCH (a:Estabelecimento)-[r1:DECLAROU_CONTATO {licitacao: '087|1308022021PE'}]->(x:Endereco)"
        "<-[r2:DECLAROU_CONTATO {licitacao: '087|1308022021PE'}]-(b:Estabelecimento) WHERE a.cnpj < b.cnpj "
        "RETURN a.cnpj AS a, b.cnpj AS b, x.id AS endereco, toString(r1.data) AS data, r1.fonte AS fonte",
    )
    assert par == [
        {
            "a": "27499707000140",
            "b": "32490833000174",
            "endereco": "61760000|411|ZEZITO",
            "data": "2021-08-10",
            "fonte": "tce_505",
        }
    ]

    # Contato só de licitação de fato: a adesão 005/2021-PC não gera contato.
    assert (
        ler(
            neo4j_teste, "MATCH ()-[r:DECLAROU_CONTATO {licitacao: '087|0052021PC'}]->() RETURN count(r) AS n"
        )[0]["n"]
        == 0
    )

    # Contrato original de 2024 ligado à licitação e ao fornecedor.
    cadeia = ler(
        neo4j_teste,
        "MATCH (e:Estabelecimento)-[:CONTRATADO_EM]->(c:Contrato)"
        "-[:ORIGINADO_DE]->(l:Licitacao {id: '087|2603012024PE'}) "
        "OPTIONAL MATCH (e)-[:PARTICIPOU_DE]->(l) RETURN count(DISTINCT c) AS contratos, count(*) AS linhas",
    )[0]
    assert cadeia["contratos"] > 0


def test_cli_grava_e_registra_carga(neo4j_teste, tmp_path):
    env = tmp_path / "teste.env"
    env.write_text(
        f"NEO4J_URI={os.environ.get('NEO4J_TEST_URI', 'bolt://localhost:7700')}\n"
        f"NEO4J_USER={os.environ.get('NEO4J_TEST_USER', 'neo4j')}\n"
        f"NEO4J_PASSWORD={os.environ.get('NEO4J_TEST_PASSWORD', 'sarc-teste-local')}\n"
        f"TCE_CACHE_PATH={tmp_path / 'cache'}\n"
    )
    argumentos = [
        "--env-file",
        str(env),
        "etl-tce",
        "--municipio",
        "087",
        "--desde",
        "2021",
        "--ate",
        "2021",
        "--so-cache",
        "--cache-extra",
        str(FIXTURE),
    ]
    resultado = CliRunner().invoke(grupo, argumentos, catch_exceptions=False)
    assert resultado.exit_code == 0, resultado.output
    carga = ler(
        neo4j_teste,
        "MATCH (c:Carga {etapa: 'etl-tce'}) RETURN c.status AS status, c.municipios AS m, "
        "c.desde AS desde, c.contagensJson AS contagens",
    )
    assert len(carga) == 1
    assert carga[0]["status"] == "concluida" and carga[0]["m"] == ["087"] and carga[0]["desde"] == 2021
    assert '"licitante_pf_descartado": 1' in carga[0]["contagens"]
