"""Etapa `sarc vinculo-aproximado` no banco de teste: grava a aresta e a consulta de conluio a lê (D-47)."""

from __future__ import annotations

from datetime import date

import pytest

from sarc.conluio.aproximado import APROXIMADO, PISTA
from sarc.conluio.carga_aproximado import executar
from sarc.conluio.consultas import PRINCIPAL, Escopo, buscar_conluio
from sarc.grafo.conexao import ler
from tests.conluio.grafo import A, B, C, Grafo, par_simples


@pytest.fixture
def g(neo4j_teste):
    return Grafo(neo4j_teste)


def _ocorrencias(driver, licitacao="087|P"):
    return buscar_conluio(driver, Escopo.licitacao(licitacao)).ocorrencias


def test_telefone_cortado_vira_vinculo_aproximado(g, neo4j_teste):
    """O caso de Caucaia: '(81)3093331' (cortado em 11 caracteres) x '08130933317'."""
    par_simples(g)
    g.contato_bruto(A, "087|P", fones=["(81)3093331"]).contato_bruto(B, "087|P", fones=["08130933317"])
    contagens = executar(neo4j_teste)
    assert contagens["arestas_gravadas"] == 1
    (o,) = _ocorrencias(neo4j_teste)
    assert o.classe == APROXIMADO
    assert (o.vinculo.tipoVinculo, o.vinculo.padrao, o.vinculo.grauContato) == (
        "telefone_aproximado",
        "prefixo",
        2,
    )
    carga = ler(neo4j_teste, "MATCH (c:Carga {etapa: 'vinculo-aproximado'}) RETURN c.status AS s")
    assert carga == [{"s": "concluida"}]


def test_endereco_com_digitacao_e_sala_diferente(g, neo4j_teste):
    par_simples(g)
    g.empresa(C).participa(C, "087|P", date(2020, 6, 1))
    g.contato_bruto(A, "087|P", enderecos=["RUA PEDRO PEREIRA, 120 SALA 1"], ceps=["60110000"])
    g.contato_bruto(B, "087|P", enderecos=["R PEDRO PERERA, 120"], ceps=["60110010"])
    g.contato_bruto(C, "087|P", enderecos=["RUA PEDRO PEREIRA, 120 SALA 2"], ceps=["60110020"])
    executar(neo4j_teste)
    por_par = {(o.cnpjA, o.cnpjB): o for o in _ocorrencias(neo4j_teste)}
    assert por_par[tuple(sorted((A, B)))].classe == APROXIMADO
    assert por_par[tuple(sorted((A, C)))].classe == PISTA
    assert por_par[tuple(sorted((A, C)))].vinculo.tipoVinculo == "mesmo_predio"


def test_recalculo_apaga_as_arestas_antigas(g, neo4j_teste):
    par_simples(g)
    g.contato_bruto(A, "087|P", fones=["(81)3093331"]).contato_bruto(B, "087|P", fones=["08130933317"])
    executar(neo4j_teste)
    g.contato_bruto(B, "087|P", fones=["08599990000"])
    assert executar(neo4j_teste)["arestas_gravadas"] == 0
    assert _ocorrencias(neo4j_teste) == []


def test_aproximado_nao_muda_o_principal(g, neo4j_teste):
    par_simples(g)
    g.socio("p", A[:8], date(2010, 1, 1)).socio("p", B[:8], date(2011, 1, 1))
    g.contato_bruto(A, "087|P", fones=["(81)3093331"]).contato_bruto(B, "087|P", fones=["08130933317"])
    executar(neo4j_teste)
    (o,) = _ocorrencias(neo4j_teste)
    assert o.classe == PRINCIPAL
    assert [v.nivel for v in o.vinculos] == [PRINCIPAL, APROXIMADO]


def test_licitacao_que_nao_e_de_fato_fica_fora(g, neo4j_teste):
    par_simples(g, de_fato=False)
    g.contato_bruto(A, "087|P", fones=["(81)3093331"]).contato_bruto(B, "087|P", fones=["08130933317"])
    assert executar(neo4j_teste)["arestas_gravadas"] == 0
