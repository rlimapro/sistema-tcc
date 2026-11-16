"""Benchmark no formato do trabalho de referência (D-43): as quatro formas dão o mesmo vínculo.

O teste de integração precisa do Neo4j de teste e do PostgreSQL do perfil bench
(docker compose --profile bench up -d postgres-bench); sem o PostgreSQL, é pulado.
"""

import os

import psycopg
import pytest

from benchmark import dados
from benchmark.referencia import (
    Par,
    Resultado,
    _descrever_erro,
    conferir,
    escolher_pares,
    gerar_relatorio,
    medir_par,
)
from sarc.grafo.conexao import escrever

DSN = os.environ.get("BENCH_PG_DSN", "postgresql://sarc:sarc-bench-local@localhost:5440/sarc")

# Empresas A, B, C, D e a holding H; pessoas P1, P2, P3. Rotas simples de até 6 saltos:
#   A–C: A-P3-H-C (3) e A-P1-B-P2-C (4)            -> 2 rotas, menor 3, 1 menor caminho
#   A–B: A-P1-B (2) e A-P3-H-C-P2-B (5)            -> 2 rotas, menor 2, 1 menor caminho
#   A–D: A-P3-H-D (3) e A-P1-B-P2-C-H-D (6)        -> 2 rotas, menor 3
#   B–E: E sem vínculo                             -> 0 rotas, sem menor
GRAFO = """
CREATE (a:Empresa {cnpjBasico: 'AAAAAAAA'}), (b:Empresa {cnpjBasico: 'BBBBBBBB'}),
       (c:Empresa {cnpjBasico: 'CCCCCCCC'}), (d:Empresa {cnpjBasico: 'DDDDDDDD'}),
       (e:Empresa {cnpjBasico: 'EEEEEEEE'}), (h:Empresa {cnpjBasico: 'HHHHHHHH'})
CREATE (p1:Pessoa {id: 'P1'}), (p2:Pessoa {id: 'P2'}), (p3:Pessoa {id: 'P3'})
CREATE (p1)-[:SOCIO]->(a), (p1)-[:SOCIO]->(b),
       (p2)-[:SOCIO]->(b), (p2)-[:SOCIO]->(c),
       (p3)-[:SOCIO]->(a), (p3)-[:SOCIO]->(h),
       (h)-[:SOCIO_PJ]->(c), (h)-[:SOCIO_PJ]->(d)
"""

ESPERADO = {  # par -> (rotas, menor, nº de menores caminhos)
    ("AAAAAAAA", "CCCCCCCC"): (2, 3, 1),
    ("AAAAAAAA", "BBBBBBBB"): (2, 2, 1),
    ("AAAAAAAA", "DDDDDDDD"): (2, 3, 1),
    ("BBBBBBBB", "EEEEEEEE"): (0, None, 0),
}


@pytest.fixture
def postgres():
    try:
        conexao = psycopg.connect(DSN, connect_timeout=3)
    except psycopg.OperationalError:
        pytest.skip(
            "PostgreSQL do benchmark indisponível (docker compose --profile bench up -d postgres-bench)"
        )
    esquema = f"teste_ref_{os.getpid()}"
    conexao.execute(f"DROP SCHEMA IF EXISTS {esquema} CASCADE")
    conexao.execute(f"CREATE SCHEMA {esquema}")
    conexao.execute(f"SET search_path TO {esquema}")
    conexao.commit()
    yield conexao, esquema
    conexao.rollback()
    conexao.execute(f"DROP SCHEMA {esquema} CASCADE")
    conexao.commit()
    conexao.close()


@pytest.mark.integration
def test_quatro_formas_dao_o_mesmo_vinculo(neo4j_teste, postgres, tmp_path) -> None:
    conexao, esquema = postgres
    escrever(neo4j_teste, GRAFO)
    dados.exportar_csv(neo4j_teste, tmp_path)
    dados.carregar_postgres(conexao, tmp_path)
    conexao.execute(f"SET search_path TO {esquema}")
    for (a, b), (rotas, menor, caminhos) in ESPERADO.items():
        resultados = medir_par(
            neo4j_teste, conexao, Par(a, b, 1, 1, "grau"), timeout=30, repeticoes=2, rapido=10
        )
        por = {r.variante: r for r in resultados}
        assert all(r.erro is None for r in resultados), [r.erro for r in resultados]
        assert por["neo4j_todas"].rotas == por["pg_todas"].rotas == rotas, (a, b)
        assert {r.menor for r in resultados} == {menor}, (a, b)
        assert por["neo4j_menor"].rotas == caminhos, (a, b)
        assert len(por["neo4j_menor"].tempos) == 2  # rápida: repetida
        assert conferir(resultados) == "sim"


def test_escolher_pares_por_grau_e_sorteio_reprodutivel() -> None:
    graus = {"A": 9, "B": 8, "C": 7, "D": 1, "E": 1, "F": 1, "G": 1}
    copart = {
        "A": ["B", "C"],
        "B": ["A", "C"],
        "C": ["A", "B"],
        "D": ["E"],
        "E": ["D", "F"],
        "F": ["E", "G"],
        "G": ["F"],
    }

    def coparticipantes(cnpj):
        return [(c, graus[c]) for c in copart[cnpj]]

    pares = escolher_pares(graus, coparticipantes, n_grau=1, n_sorteio=2, semente=7)
    assert (pares[0].a, pares[0].b, pares[0].origem) == ("A", "B", "grau")
    assert [p.origem for p in pares[1:]] == ["sorteio", "sorteio"]
    usados = [c for p in pares for c in (p.a, p.b)]
    assert len(usados) == len(set(usados))  # nenhuma empresa repetida
    assert pares == escolher_pares(graus, coparticipantes, n_grau=1, n_sorteio=2, semente=7)


def test_conferencia_e_erros() -> None:
    def r(v, rotas, menor, erro=None):
        return Resultado(v, [0.1], rotas, menor, erro)

    iguais = [r("neo4j_menor", 1, 3), r("pg_menor", None, 3), r("pg_todas", 2, 3), r("neo4j_todas", 2, 3)]
    assert conferir(iguais) == "sim"
    assert conferir(iguais[:3] + [r("neo4j_todas", 5, 3)]).startswith("divergente: rotas")
    assert conferir([r("neo4j_menor", 1, 3), r("pg_menor", None, 4)]).startswith("divergente: menor")
    assert conferir([r("neo4j_menor", 1, 3), r("pg_todas", None, None, "não terminou em 300 s")]) == "-"
    assert _descrever_erro(RuntimeError("Transaction TimedOut"), 300) == "não terminou em 300 s"
    assert (
        _descrever_erro(RuntimeError("canceling statement due to statement timeout"), 60)
        == "não terminou em 60 s"
    )
    assert _descrever_erro(RuntimeError("MemoryLimitExceeded"), 60) == "falta de memória"


def test_relatorio_declara_a_diferenca_de_escala() -> None:
    pares = [Par("A", "B", 3, 2, "grau")]
    medidas = [[Resultado(v, [0.05], 1, 2) for v in ("neo4j_menor", "pg_menor", "pg_todas", "neo4j_todas")]]
    texto = gerar_relatorio(
        pares,
        medidas,
        {"empresas": 65_571, "pessoas": 66_085, "sociedades": 100_000},
        {"CPU": "x"},
        {"n_grau": 1, "n_sorteio": 0, "semente": 42, "timeout": 300, "repeticoes": 5, "rapido": 10},
        __import__("datetime").datetime(2026, 11, 1),
    )
    assert "A base aqui é muito menor que a do artigo:" in texto
    assert "7.754.989" in texto and "65.571" in texto
    assert "| 1 | grau | 3 | 2 | 2 |" in texto
