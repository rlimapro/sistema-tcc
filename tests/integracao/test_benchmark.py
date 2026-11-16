"""Q1-Q4 devolvem o mesmo resultado no Neo4j e no PostgreSQL, e o resultado esperado, num grafo sintético.

Precisa do Neo4j de teste e do PostgreSQL do perfil bench
(docker compose --profile bench up -d postgres-bench). As tabelas vão para um schema próprio.
"""

import os
from datetime import date

import psycopg
import pytest

from benchmark import dados, medir
from benchmark.consultas import CONSULTAS, CYPHER_LICITACAO_REFERENCIA, Q0, Q1, Q2, Q3, Q4
from sarc.grafo.conexao import escrever, ler

pytestmark = pytest.mark.integration

DSN = os.environ.get("BENCH_PG_DSN", "postgresql://sarc:sarc-bench-local@localhost:5440/sarc")

A, A2, B, C, D = "11111111000101", "11111111000202", "22222222000101", "33333333000101", "44444444000101"

GRAFO = """
CREATE (l1:Licitacao {id: 'L1', dataRealizacao: date('2020-06-01')})
CREATE (l2:Licitacao {id: 'L2', dataRealizacao: date('2018-01-01')})
WITH l1, l2
UNWIND $estabs AS cnpj
MERGE (e:Estabelecimento {cnpj: cnpj})
MERGE (emp:Empresa {cnpjBasico: left(cnpj, 8)})
MERGE (e)-[:PERTENCE_A]->(emp)
WITH DISTINCT l1, l2
MATCH (a:Estabelecimento {cnpj: $A}), (a2:Estabelecimento {cnpj: $A2}), (b:Estabelecimento {cnpj: $B}),
      (c:Estabelecimento {cnpj: $C}), (d:Estabelecimento {cnpj: $D})
MATCH (ea:Empresa {cnpjBasico: '11111111'}), (eb:Empresa {cnpjBasico: '22222222'}),
      (ec:Empresa {cnpjBasico: '33333333'}), (ed:Empresa {cnpjBasico: '44444444'})
CREATE (h:Empresa {cnpjBasico: '99999999'})
CREATE (a)-[:PARTICIPOU_DE]->(l1), (a2)-[:PARTICIPOU_DE]->(l1), (b)-[:PARTICIPOU_DE]->(l1),
       (c)-[:PARTICIPOU_DE]->(l1), (a)-[:PARTICIPOU_DE]->(l2), (d)-[:PARTICIPOU_DE]->(l2)
CREATE (p1:Pessoa {id: 'P1'}), (p2:Pessoa {id: 'P2'}), (p3:Pessoa {id: 'P3'})
CREATE (p1)-[:SOCIO {dataEntrada: date('2015-01-01')}]->(ea),
       (p1)-[:SOCIO {dataEntrada: date('2019-01-01')}]->(eb),
       (p2)-[:SOCIO {dataEntrada: date('2021-01-01')}]->(ec),
       (p2)-[:SOCIO {dataEntrada: date('2010-01-01')}]->(eb),
       (p3)-[:SOCIO {dataEntrada: date('2012-01-01')}]->(ea),
       (p3)-[:SOCIO {dataEntrada: date('2013-01-01')}]->(h),
       (h)-[:SOCIO_PJ {dataEntrada: date('2016-01-01')}]->(ec),
       (h)-[:SOCIO_PJ {dataEntrada: date('2017-01-01')}]->(ed)
CREATE (t:Telefone {numero: '8533334444'}), (b)-[:TEM_TELEFONE]->(t), (c)-[:TEM_TELEFONE]->(t)
CREATE (m:Email {endereco: 'x@exemplo.com'}), (a)-[:TEM_EMAIL]->(m)
CREATE (en:Endereco {id: '60000000|1'}), (a)-[:LOCALIZADO_EM]->(en), (d)-[:LOCALIZADO_EM]->(en)
"""

ESPERADO = {
    "Q0": {(1,)},
    "Q1": {(A,), (A2,), (B,), (C,)},
    "Q2": {("L1", A, B), ("L1", A2, B)},
    "Q3": {("L1", A, B, "socio"), ("L1", A2, B, "socio"), ("L1", B, C, "telefone"), ("L2", A, D, "endereco")},
    "Q4": {
        ("L1", A, B, 2),
        ("L1", A2, B, 2),
        ("L1", A, C, 3),
        ("L1", A2, C, 3),
        ("L1", B, C, 5),
        ("L2", A, D, 3),
    },
}
COLUNAS = {
    "Q0": ("um",),
    "Q1": ("cnpj",),
    "Q2": ("id", "cnpj1", "cnpj2"),
    "Q3": ("id", "cnpj1", "cnpj2", "tipo"),
    "Q4": ("id", "cnpj1", "cnpj2", "saltos"),
}


@pytest.fixture
def postgres():
    try:
        conexao = psycopg.connect(DSN, connect_timeout=3)
    except psycopg.OperationalError:
        pytest.skip(
            "PostgreSQL do benchmark indisponível (docker compose --profile bench up -d postgres-bench)"
        )
    esquema = f"teste_{os.getpid()}"
    conexao.execute(f"DROP SCHEMA IF EXISTS {esquema} CASCADE")
    conexao.execute(f"CREATE SCHEMA {esquema}")
    conexao.execute(f"SET search_path TO {esquema}")
    conexao.commit()
    yield conexao
    conexao.rollback()
    conexao.execute(f"DROP SCHEMA {esquema} CASCADE")
    conexao.commit()
    conexao.close()


def test_consultas_iguais_e_esperadas(neo4j_teste, postgres, tmp_path) -> None:
    escrever(neo4j_teste, GRAFO, {"estabs": [A, A2, B, C, D], "A": A, "A2": A2, "B": B, "C": C, "D": D})
    volumes = dados.exportar_csv(neo4j_teste, tmp_path)
    assert volumes["participou_de"] == 6 and volumes["socio"] == 6 and volumes["socio_pj"] == 2
    dados.carregar_postgres(postgres, tmp_path)
    postgres.execute(f"SET search_path TO teste_{os.getpid()}")

    referencia = ler(neo4j_teste, CYPHER_LICITACAO_REFERENCIA)[0]
    assert referencia == {"id": "L1", "n": 4}
    params = {"id": referencia["id"]}
    assert [c.nome for c in CONSULTAS] == ["Q0", "Q1", "Q2", "Q3", "Q4"]
    for consulta in (Q0, Q1, Q2, Q3, Q4):
        neo = medir.executor_neo4j(neo4j_teste, consulta, params, 30)()
        pg = medir.executor_postgres(postgres, consulta, params)()
        assert medir.impressao(neo) == medir.impressao(pg), consulta.nome
        obtido = {tuple(linha[c] for c in COLUNAS[consulta.nome]) for linha in neo}
        assert obtido == ESPERADO[consulta.nome], consulta.nome
        assert len(neo) == len(ESPERADO[consulta.nome])


def test_medidas_e_percentil() -> None:
    assert medir.percentil([5, 1, 3, 2, 4], 95) == 5
    assert medir.percentil(list(range(1, 101)), 95) == 95
    medida = medir.Medida(banco="neo4j", consulta="Q1")
    medir.medir_quente(medida, lambda: [{"x": 1}], aquecimento=2, repeticoes=5)
    assert len(medida.quente) == 5 and medida.linhas == 1 and medida.erro is None
    assert medir.impressao([{"a": 1, "b": date(2020, 1, 1)}]) == medir.impressao(
        [{"b": "2020-01-01", "a": "1"}]
    )
