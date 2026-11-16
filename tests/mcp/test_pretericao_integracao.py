"""Tools de preterição do MCP contra o Neo4j de teste: "não calculada" × zero (D-36), componentes
principal e auxiliar separados, escolha da janela, tetos (F-18), erros legíveis e READ_ACCESS (F-04).
O grafo é gravado pela própria carga da preterição (`gravar_municipio`), nunca lido do cache (F-20)."""

from __future__ import annotations

import json

import pytest
from neo4j import READ_ACCESS

from sarc.grafo.conexao import escrever
from sarc.mcp_server import ferramentas
from sarc.pretericao.consultas import GARANTIAS_DE_METODO
from tests.pretericao.dados import CREDOR_A, CREDOR_B, cenario_dois_componentes, gravar

pytestmark = pytest.mark.integration


@pytest.fixture
def grafo(neo4j_teste):
    for codigo, nome in {"074": "Iguatu", "087": "Itatira"}.items():
        escrever(neo4j_teste, "MERGE (m:Municipio {codigo: $c}) SET m.nome = $n", {"c": codigo, "n": nome})
    return neo4j_teste


@pytest.fixture
def iguatu(grafo):
    gravar(grafo, "074", "2025+0", cenario_dois_componentes())
    return grafo


def _erro_ou_resposta(funcao, *args):
    try:
        return funcao(*args)
    except Exception as erro:  # noqa: BLE001
        return ferramentas.erro_para_resposta(erro)


# "não calculada" × zero ------------------------------------------------------------------------
def test_municipio_nao_calculado_nao_e_zero(grafo):
    r = ferramentas.ranking_de_preteridos(grafo, "Itatira", None, None)
    d = r["dados"]
    assert d["calculado"] is False
    assert "rankingPrincipal" not in d and "listaAuxiliarNaoPagas" not in d
    assert r["limiteDeLeitura"].startswith("A preterição NÃO foi calculada")
    assert r["garantiasDeMetodo"] == GARANTIAS_DE_METODO


def test_municipio_calculado_sem_preterição_e_zero(grafo):
    gravar(grafo, "087", "2025+0", ([], [], [], []))
    r = ferramentas.ranking_de_preteridos(grafo, "087", None, None)
    d = r["dados"]
    assert d["calculado"] is True
    assert d["rankingPrincipal"] == [] and d["listaAuxiliarNaoPagas"] == []
    assert "NÃO foi calculada" not in r["limiteDeLeitura"]


def test_exercicio_nao_calculado_num_municipio_calculado(iguatu):
    r = ferramentas.ranking_de_preteridos(iguatu, "074", "2024", None)
    assert r["dados"]["calculado"] is False
    assert r["dados"]["janelasDisponiveis"] == ["2025+0"]
    assert r["limiteDeLeitura"].startswith("A preterição NÃO foi calculada")


# listas separadas ------------------------------------------------------------------------------
def test_principal_e_auxiliar_separados_sem_total(iguatu):
    r = ferramentas.ranking_de_preteridos(iguatu, "Iguatu", "2025", None)
    d = r["dados"]
    assert d["calculado"] is True and d["janela"] == "2025+0"
    assert d["municipio"] == {"codigo": "074", "nome": "Iguatu"}
    assert d["rankingPrincipal"][0]["cnpj"] == CREDOR_B
    assert [c["cnpj"] for c in d["listaAuxiliarNaoPagas"]] == [CREDOR_A]
    assert d["orgaos"] and d["orgaos"][0]["nObrigacoes"] == 17
    chaves = set(d) | {k for c in d["rankingPrincipal"] + d["listaAuxiliarNaoPagas"] for k in c}
    assert not any("total" in k.lower() for k in chaves)
    assert "credor PRETERIDO" in r["limiteDeLeitura"]
    assert "FilaPagamento" in r["consultaExecutada"]
    assert r["regime"]["parametros"]["janela"] == "2025+0"
    json.dumps(r)


def test_duas_janelas_do_mesmo_exercicio_pedem_escolha(iguatu):
    gravar(iguatu, "074", "2025+3", cenario_dois_componentes())
    r = ferramentas.ranking_de_preteridos(iguatu, "074", "2025", None)
    assert r["dados"]["janelasDisponiveis"] == ["2025+0", "2025+3"]
    assert "rankingPrincipal" not in r["dados"]
    escolhida = ferramentas.ranking_de_preteridos(iguatu, "074", "2025+3", None)
    assert escolhida["dados"]["janela"] == "2025+3" and escolhida["dados"]["rankingPrincipal"]


def test_limite_corta_e_tem_teto(iguatu):
    r = ferramentas.ranking_de_preteridos(iguatu, "074", None, 1)
    d = r["dados"]
    assert len(d["rankingPrincipal"]) == 1
    assert d["listasTruncadas"] is True and d["credoresAcimaDoPiso"] == 2
    assert "CORTADAS" in r["limiteDeLeitura"]
    r = ferramentas.ranking_de_preteridos(iguatu, "074", None, 10_000)
    assert r["dados"]["listasCortadasEm"] == ferramentas.LIMITE_MAXIMO
    assert r["dados"]["listasTruncadas"] is False


@pytest.mark.parametrize(("municipio", "exercicio"), [("Nenhum", None), ("074", "25/2025"), ("", None)])
def test_entrada_invalida_vira_erro(iguatu, municipio, exercicio):
    r = _erro_ou_resposta(ferramentas.ranking_de_preteridos, iguatu, municipio, exercicio, None)
    assert r["erro"] == "parametro_invalido"


# credor ----------------------------------------------------------------------------------------
def test_credor_nas_filas(iguatu):
    r = ferramentas.pretericao_do_credor(iguatu, "11.111.111/0001-11", None)
    d = r["dados"]
    assert d["calculado"] is True and d["nFilas"] == 1
    assert d["filas"][0]["credorNaFila"]["nNaoPagas"] == 5
    assert d["municipiosCalculados"] == ["074"]


def test_credor_ausente_das_filas_calculadas(iguatu):
    d = ferramentas.pretericao_do_credor(iguatu, "99999999000199", None)["dados"]
    assert d["calculado"] is True and d["filas"] == []
    assert "não aparece" in d["situacao"]


def test_credor_sem_nenhum_municipio_calculado(grafo):
    r = ferramentas.pretericao_do_credor(grafo, CREDOR_A, None)
    assert r["dados"]["calculado"] is False
    assert r["limiteDeLeitura"].startswith("A preterição NÃO foi calculada")


def test_credor_com_cnpj_invalido(grafo):
    assert (
        _erro_ou_resposta(ferramentas.pretericao_do_credor, grafo, "123", None)["erro"]
        == "parametro_invalido"
    )


def test_municipios_com_pretericao(iguatu):
    r = ferramentas.municipios_com_pretericao(iguatu)
    assert [m["municipio"] for m in r["dados"]["municipiosCalculados"]] == ["074"]
    assert r["dados"]["nMunicipiosCalculados"] == 1
    assert "não é o mesmo que zero" in r["limiteDeLeitura"]


# F-04 ------------------------------------------------------------------------------------------
def test_tools_de_pretericao_so_abrem_sessao_de_leitura(iguatu, monkeypatch):
    modos: list[str | None] = []
    original = type(iguatu).session

    def espiao(self, **kwargs):
        modos.append(kwargs.get("default_access_mode"))
        return original(self, **kwargs)

    monkeypatch.setattr(type(iguatu), "session", espiao)
    ferramentas.ranking_de_preteridos(iguatu, "074", None, None)
    ferramentas.pretericao_do_credor(iguatu, CREDOR_A, None)
    ferramentas.municipios_com_pretericao(iguatu)
    assert modos and all(m == READ_ACCESS for m in modos)
