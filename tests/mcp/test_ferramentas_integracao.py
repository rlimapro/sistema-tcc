"""Tools do MCP contra o Neo4j de teste: somente leitura de verdade (F-04), tetos (F-18), máscara
(F-17), listas separadas (F-05) e regime lido dos nós :Carga (F-19)."""

from __future__ import annotations

import json
from datetime import date

import pytest

from sarc.grafo.conexao import escrever, ler
from sarc.mcp_server import ferramentas
from tests.conluio.grafo import A, B, C, Grafo, itatira, par_simples

pytestmark = pytest.mark.integration

GRAU = 5


@pytest.fixture
def g(neo4j_teste):
    return Grafo(neo4j_teste)


# F-04: a recusa vem do banco -------------------------------------------------------------------
@pytest.mark.parametrize(
    "consulta",
    [
        "ALTER CURRENT USER SET PASSWORD FROM 'sarc-teste-local' TO 'outra-senha-123'",
        "ALTER CURRENT USER SET\nPASSWORD FROM 'sarc-teste-local' TO 'outra-senha-123'",
        "CREATE (n:Intrusa) RETURN n",
        "MATCH (n) DETACH DELETE n",
    ],
)
def test_escrita_e_troca_de_senha_sao_recusadas_pelo_banco(neo4j_teste, g, consulta):
    g.municipio()
    resposta = _erro_ou_resposta(ferramentas.cypher_somente_leitura, neo4j_teste, consulta, None)
    assert resposta["erro"] == "escrita_recusada_pelo_banco"
    assert resposta["codigo"] == "Neo.ClientError.Statement.AccessMode"
    # o banco continua igual e a senha não mudou (o driver de teste segue autenticando)
    assert ler(neo4j_teste, "MATCH (n) RETURN count(n) AS n")[0]["n"] == 1


def _erro_ou_resposta(funcao, *args):
    try:
        return funcao(*args)
    except Exception as erro:  # noqa: BLE001
        return ferramentas.erro_para_resposta(erro)


@pytest.mark.parametrize(
    "consulta",
    [
        "LOAD CSV FROM 'http://127.0.0.1:9/x.csv' AS l RETURN l",
        "load  csv with headers from 'file:///etc/passwd' AS l RETURN l",
        "TERMINATE TRANSACTIONS 'neo4j-transaction-1'",
        "CALL dbms.killConnections(['bolt-1'])",
        "CALL dbms.listConfig()",
        "CALL db.clearQueryCaches()",
        "CALL `dbms`.listConfig() YIELD name RETURN count(*) AS n",
        "CALL dbms/**/.listConfig() YIELD name RETURN count(*) AS n",
        'TERMINATE /* x */ TRANSACTIONS "neo4j-transaction-999999"',
        'LOAD/**/CSV FROM "http://127.0.0.1:9/x" AS l RETURN l',
        "CALL db.`clearQueryCaches`()",
        "LOAD // comentário\nCSV FROM 'http://127.0.0.1:9/x' AS l RETURN l",
        "WITH 'http://127.0.0.1:9/x' AS u LOAD CSV FROM u AS l RETURN l",
        "WITH '/*' AS a CALL dbms.listConfig() YIELD name WITH a, count(*) AS n RETURN a, n, '*/' AS b",
        "WITH 'x' AS u LOAD   CSV FROM u AS l RETURN l",
    ],
)
def test_comandos_aceitos_em_leitura_mas_nocivos_sao_recusados_antes(neo4j_teste, consulta):
    r = ferramentas.cypher_somente_leitura(neo4j_teste, consulta, None)
    assert r["erro"] == "consulta_nao_permitida"


# F-18: tetos de linhas e de caracteres -------------------------------------------------------------
def test_cypher_livre_corta_em_200_linhas_mesmo_pedindo_mais(neo4j_teste):
    escrever(neo4j_teste, "UNWIND range(1, 250) AS i CREATE (:Numero {i: i})")
    r = ferramentas.cypher_somente_leitura(neo4j_teste, "MATCH (n:Numero) RETURN n.i AS i", 10_000)
    assert r["dados"]["nLinhas"] == 200
    assert r["dados"]["truncado"] is True
    assert "TRUNCADO" in r["limiteDeLeitura"]
    assert r["garantiasDeMetodo"] == {}


def test_cypher_livre_corta_por_caracteres(neo4j_teste):
    escrever(
        neo4j_teste,
        "UNWIND range(1, 100) AS i CREATE (:Texto {t: reduce(s = '', x IN range(1, 1000) | s + 'x')})",
    )
    r = ferramentas.cypher_somente_leitura(neo4j_teste, "MATCH (n:Texto) RETURN n.t AS t", None)
    assert r["dados"]["truncado"] is True
    assert len(json.dumps(r["dados"]["linhas"])) <= ferramentas.TETO_CARACTERES
    assert "caracteres" in r["dados"]["motivoDoCorte"]


def test_cypher_livre_sem_corte_nao_marca_truncado(neo4j_teste, g):
    g.municipio()
    r = ferramentas.cypher_somente_leitura(neo4j_teste, "MATCH (m:Municipio) RETURN m", None)
    assert r["dados"]["truncado"] is False
    assert r["dados"]["linhas"][0]["m"]["_rotulos"] == ["Municipio"]


def test_cypher_livre_mascara_no_de_telefone(neo4j_teste, g):
    par_simples(g)
    g.telefone_rfb(A, "85988887777")
    r = ferramentas.cypher_somente_leitura(neo4j_teste, "MATCH (t:Telefone) RETURN t", None)
    assert "85988887777" not in json.dumps(r)


def test_limite_das_listas_de_conluio_tem_teto(neo4j_teste, g):
    itatira(g, antecipada=True)
    r = ferramentas.licitacoes_com_conluio(neo4j_teste, "087", None, None, 10_000, GRAU)
    assert r["dados"]["listasCortadasEm"] == ferramentas.LIMITE_MAXIMO


# Tools validadas -------------------------------------------------------------------------------------
def test_conluio_na_licitacao_devolve_listas_separadas_e_cobertura(neo4j_teste, g):
    itatira(g)
    r = ferramentas.conluio_na_licitacao(neo4j_teste, "087|L2", GRAU)
    d = r["dados"]
    assert d["alertas"] == []
    assert d["alertasConfiancaDesconhecida"] == []
    [posterior] = d["vinculosPosterioresAoCertame"]
    assert posterior["tipoVinculo"] == "cadeia_societaria"
    assert posterior["dataVinculo"] == "2025-07-30"
    assert posterior["dataCertame"] == "2021-08-10"
    assert d["cobertura"] == [
        {
            "municipio": "087",
            "ano": 2021,
            "certamesDeFato": 1,
            "com2OuMaisParticipantes": 1,
            "proporcaoCom2OuMais": 1.0,
        }
    ]
    assert "Cobertura" in r["limiteDeLeitura"]
    assert "shortestPath" in r["consultaExecutada"]
    # a pessoa sócia sai como token, nunca com o id gravado no grafo
    pessoas = [p["pessoa"] for p in posterior["vinculos"][0]["caminho"] if "pessoa" in p]
    assert pessoas and all(p.startswith("pessoa:") for p in pessoas)
    assert '"p1"' not in json.dumps(r)


def test_conluio_na_licitacao_contato_sai_mascarado(neo4j_teste, g):
    par_simples(g)
    g.declarou_telefone(A, "88999990000", "087|P", date(2020, 6, 1))
    g.declarou_telefone(B, "88999990000", "087|P", date(2020, 6, 1))
    r = ferramentas.conluio_na_licitacao(neo4j_teste, "087|P", GRAU)
    [alerta] = r["dados"]["alertas"]
    assert alerta["vinculos"][0]["contato"].startswith("contato:")
    assert "88999990000" not in json.dumps(r)


def test_licitacao_inexistente_vira_erro_legivel(neo4j_teste):
    r = _erro_ou_resposta(ferramentas.conluio_na_licitacao, neo4j_teste, "999|NADA", GRAU)
    assert r["erro"] == "parametro_invalido"


def test_licitacoes_com_conluio_aceita_nome_do_municipio(neo4j_teste, g):
    itatira(g, antecipada=True)
    r = ferramentas.licitacoes_com_conluio(neo4j_teste, "itatirá", "2021", "2021", None, GRAU)
    assert r["dados"]["municipio"]["codigo"] == "087"
    assert [a["licitacao"] for a in r["dados"]["alertas"]] == ["087|L2"]
    assert r["regime"]["parametros"]["desde"] == "2021-01-01"


def test_vinculos_da_empresa_agrega_por_empresa_relacionada(neo4j_teste, g):
    itatira(g, antecipada=True)
    r = ferramentas.vinculos_da_empresa(neo4j_teste, A, None, GRAU)
    relacionadas = {x["cnpj"]: x for x in r["dados"]["empresasRelacionadasQueDisputaramJunto"]}
    assert set(relacionadas) == {B, C}
    assert relacionadas[C]["certamesComAlerta"] == 1
    assert relacionadas[C]["tiposDeVinculo"] == ["cadeia_societaria"]


def test_cobertura_dos_dados(neo4j_teste, g):
    itatira(g)
    r = ferramentas.cobertura_dos_dados(neo4j_teste, "Itatira", None, None)
    assert [x["ano"] for x in r["dados"]["cobertura"]] == [2019, 2021]


def test_resumo_da_carga_le_os_nos_carga(neo4j_teste, g):
    itatira(g)
    g.carga("etl-tce", {"municipios": ["087"], "desde": 2019, "ate": 2021})
    r = ferramentas.resumo_da_carga(neo4j_teste)
    assert r["regime"]["cargas"]["municipios"] == ["087"]
    assert r["regime"]["cargas"]["recortes"][0]["desde"] == 2019  # janela por carga (F-19)
    assert r["dados"]["cargas"][0]["etapa"] == "etl-tce"
    assert r["regime"]["semRegistroDeCarga"] is False
    assert r["dados"]["nos"]["Licitacao"] == 2
    assert r["dados"]["arestas"]["SOCIO"] == 4


def test_esquema_do_grafo(neo4j_teste, g):
    itatira(g)
    r = ferramentas.esquema_do_grafo(neo4j_teste)
    assert "dataRealizacao" in r["dados"]["nos"]["Licitacao"]
    assert "dataEntrada" in r["dados"]["arestas"]["SOCIO"]


def test_nenhum_campo_de_total_somado(neo4j_teste, g):
    itatira(g)
    g.telefone_rfb(A, "8530000000").telefone_rfb(C, "8530000000")
    r = ferramentas.licitacoes_com_conluio(neo4j_teste, "087", None, None, None, GRAU)
    chaves = set(r["dados"]) | set(r["dados"]["contagens"])
    assert not any("total" in k.lower() for k in chaves)
