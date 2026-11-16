"""Consulta de conluio contra o Neo4j de teste: os casos (a)-(f) do PLANO (T06) e os furos vizinhos.

Cada teste falha se a regra correspondente for revertida (padrão herdado do v2).
"""

from __future__ import annotations

from datetime import date

import pytest

from sarc.conluio.consultas import (
    AUXILIAR,
    CONFIRMADA_NO_CERTAME,
    DESCONHECIDA,
    ENTRADA_ANTERIOR,
    ENTRADA_POSTERIOR,
    PRINCIPAL,
    Escopo,
    buscar_conluio,
    cobertura,
)
from sarc.dominio.qualificacoes import QUALIFICACOES_SOCIETARIAS
from tests.conluio.grafo import A, B, C, D, Grafo, itatira, par_simples

pytestmark = pytest.mark.integration


@pytest.fixture
def g(neo4j_teste):
    return Grafo(neo4j_teste)


def _uma(resultado):
    assert len(resultado.ocorrencias) == 1, resultado.ocorrencias
    return resultado.ocorrencias[0]


# (a) sócio que entrou depois do certame não entra no principal --------------------------------
def test_a_socio_que_entrou_depois_do_certame_fica_fora_do_principal(g, neo4j_teste):
    par_simples(g, data=date(2020, 6, 1))
    g.socio("p", A[:8], date(2010, 1, 1)).socio("p", B[:8], date(2020, 6, 2))
    o = _uma(buscar_conluio(neo4j_teste, Escopo.licitacao("087|P")))
    assert o.nivel == AUXILIAR
    assert o.classe == ENTRADA_POSTERIOR
    assert o.vinculo.tipoVinculo == "socio_pf"
    assert o.vinculo.dataVinculo == date(2020, 6, 2)


def test_a_socio_que_entrou_no_dia_do_certame_ou_antes_entra_no_principal(g, neo4j_teste):
    par_simples(g, data=date(2020, 6, 1))
    g.socio("p", A[:8], date(2010, 1, 1)).socio("p", B[:8], date(2020, 6, 1))
    o = _uma(buscar_conluio(neo4j_teste, Escopo.licitacao("087|P")))
    assert (o.nivel, o.vinculo.tipoVinculo, o.vinculo.confiancaTemporal) == (
        PRINCIPAL,
        "socio_pf",
        ENTRADA_ANTERIOR,
    )
    assert o.vinculo.saltos == 2


def test_a_data_de_entrada_nula_falha_fechada(g, neo4j_teste):
    par_simples(g)
    g.socio("p", A[:8], date(2010, 1, 1)).socio("p", B[:8], None)
    o = _uma(buscar_conluio(neo4j_teste, Escopo.licitacao("087|P")))
    assert o.nivel == AUXILIAR
    assert o.vinculo.confiancaTemporal == DESCONHECIDA


def test_a_data_do_certame_vem_da_participacao_antes_da_licitacao(g, neo4j_teste):
    """`PARTICIPOU_DE.dataRealizacao` prevalece: a licitação diz 2020-12-01, a participação 2020-06-01."""
    g.municipio().licitacao("087|P", date(2020, 12, 1)).empresa(A).empresa(B)
    g.participa(A, "087|P", date(2020, 6, 1)).participa(B, "087|P", date(2020, 6, 1))
    g.socio("p", A[:8], date(2010, 1, 1)).socio("p", B[:8], date(2020, 9, 1))
    o = _uma(buscar_conluio(neo4j_teste, Escopo.licitacao("087|P")))
    assert o.dataCertame == date(2020, 6, 1)
    assert o.nivel == AUXILIAR


# (b) qualificação fora da constante (procurador, 17) fica fora dos dois níveis ----------------
def test_b_qualificacao_17_fica_fora_dos_dois_niveis(g, neo4j_teste):
    assert "17" not in QUALIFICACOES_SOCIETARIAS
    par_simples(g)
    g.socio("p", A[:8], date(2010, 1, 1), "17").socio("p", B[:8], date(2010, 1, 1), "17")
    g.representa("q", A[:8], "17").representa("q", B[:8], "17")
    r = buscar_conluio(neo4j_teste, Escopo.licitacao("087|P"))
    assert r.ocorrencias == []


# (c) caminho só via REPRESENTA nunca entra no principal ----------------------------------------
def test_c_caminho_so_via_representa_fica_no_auxiliar(g, neo4j_teste):
    par_simples(g)
    g.representa("q", A[:8]).representa("q", B[:8])
    o = _uma(buscar_conluio(neo4j_teste, Escopo.licitacao("087|P")))
    assert o.nivel == AUXILIAR
    assert (o.vinculo.tipoVinculo, o.vinculo.confiancaTemporal) == ("representante", DESCONHECIDA)
    assert o.vinculo.dataVinculo is None


# (d) licitação com chave colidida (e não licitação de fato) é excluída e contada --------------
@pytest.mark.parametrize(
    ("marcas", "situacao"),
    [
        ({"colidida": True}, "chaveColidida"),
        ({"de_fato": False}, "naoLicitacaoDeFato"),
        ({"colidida": None}, "semMarcacao"),
        ({"de_fato": None}, "semMarcacao"),
    ],
)
def test_d_licitacao_excluida_e_contada(g, neo4j_teste, marcas, situacao):
    par_simples(g, **marcas)
    g.socio("p", A[:8], date(2010, 1, 1)).socio("p", B[:8], date(2010, 1, 1))
    r = buscar_conluio(neo4j_teste, Escopo.licitacao("087|P"))
    assert r.ocorrencias == []
    assert r.licitacoesPorSituacao[situacao] == 1
    assert r.licitacoesPorSituacao["avaliada"] == 0


# (e) holding direta (F-16) e holding comum -----------------------------------------------------
def test_e_holding_direta_e_achada(g, neo4j_teste):
    par_simples(g)
    g.socio_pj(A[:8], B[:8], date(2015, 1, 1))
    o = _uma(buscar_conluio(neo4j_teste, Escopo.licitacao("087|P")))
    assert (o.nivel, o.vinculo.tipoVinculo, o.vinculo.saltos) == (PRINCIPAL, "holding_direta", 1)


def test_e_holding_comum_e_socio_pj(g, neo4j_teste):
    par_simples(g)
    g.empresa(D).socio_pj(D[:8], A[:8], date(2015, 1, 1)).socio_pj(D[:8], B[:8], date(2016, 1, 1))
    o = _uma(buscar_conluio(neo4j_teste, Escopo.licitacao("087|P")))
    assert (o.nivel, o.vinculo.tipoVinculo) == (PRINCIPAL, "socio_pj")
    assert o.vinculo.dataVinculo == date(2016, 1, 1)


def test_e_holding_sem_data_fica_no_auxiliar(g, neo4j_teste):
    par_simples(g)
    g.socio_pj(A[:8], B[:8], None)
    o = _uma(buscar_conluio(neo4j_teste, Escopo.licitacao("087|P")))
    assert (o.nivel, o.vinculo.confiancaTemporal) == (AUXILIAR, DESCONHECIDA)


# (f) cadeia real de Itatira não é achada; variante com datas antecipadas é ---------------------
def test_f_cadeia_real_de_itatira_nao_e_alerta(g, neo4j_teste):
    itatira(g)
    r = buscar_conluio(neo4j_teste, Escopo.municipio("087"))
    assert r.por_classe(PRINCIPAL) == []
    posteriores = {
        (o.licitacao, o.vinculo.tipoVinculo, o.vinculo.saltos) for o in r.por_classe(ENTRADA_POSTERIOR)
    }
    assert posteriores == {("087|L1", "socio_pf", 2), ("087|L2", "cadeia_societaria", 4)}


def test_f_variante_de_itatira_com_datas_antecipadas_e_alerta(g, neo4j_teste):
    itatira(g, antecipada=True)
    r = buscar_conluio(neo4j_teste, Escopo.municipio("087"))
    alertas = {(o.licitacao, o.vinculo.tipoVinculo, o.vinculo.saltos) for o in r.por_classe(PRINCIPAL)}
    assert alertas == {("087|L1", "socio_pf", 2), ("087|L2", "cadeia_societaria", 4)}
    assert r.contagens()["paresSoNoAuxiliar"] == 0


def test_atalho_posterior_nao_esconde_cadeia_valida_mais_longa(g, neo4j_teste):
    """O shortestPath filtra aresta a aresta: uma holding direta de 2024 (posterior) não pode
    impedir que a cadeia válida de 4 saltos seja achada para o certame de 2021."""
    itatira(g, antecipada=True)
    g.socio_pj(A[:8], C[:8], date(2024, 1, 1))
    r = buscar_conluio(neo4j_teste, Escopo.licitacao("087|L2"))
    o = _uma(r)
    assert (o.nivel, o.vinculo.tipoVinculo, o.vinculo.saltos) == (PRINCIPAL, "cadeia_societaria", 4)


# Contato declarado no certame (505, T10) e contato da RFB --------------------------------------
def test_contato_declarado_no_mesmo_certame_e_principal(g, neo4j_teste):
    par_simples(g)
    g.declarou_telefone(A, "88999990000", "087|P", date(2020, 6, 1))
    g.declarou_telefone(B, "88999990000", "087|P", date(2020, 6, 1))
    o = _uma(buscar_conluio(neo4j_teste, Escopo.licitacao("087|P")))
    assert (o.nivel, o.vinculo.tipoVinculo, o.vinculo.fonte, o.vinculo.confiancaTemporal) == (
        PRINCIPAL,
        "telefone",
        "tce_505",
        CONFIRMADA_NO_CERTAME,
    )


def test_contato_declarado_em_outro_certame_nao_liga(g, neo4j_teste):
    par_simples(g)
    g.licitacao("087|OUTRA", date(2018, 1, 1))
    g.declarou_telefone(A, "88999990000", "087|P", date(2020, 6, 1))
    g.declarou_telefone(B, "88999990000", "087|OUTRA", date(2018, 1, 1))
    assert buscar_conluio(neo4j_teste, Escopo.licitacao("087|P")).ocorrencias == []


def test_contato_declarado_amplamente_compartilhado_vai_para_o_auxiliar(g, neo4j_teste):
    par_simples(g)
    g.declarou_telefone(A, "8533330000", "087|P", date(2020, 6, 1))
    g.declarou_telefone(B, "8533330000", "087|P", date(2020, 6, 1))
    for i in range(3):  # 5 raízes no total usam o telefone (escritório de contabilidade)
        outro = f"9{i}000000000199"
        g.empresa(outro).telefone_rfb(outro, "8533330000")
    o = _uma(buscar_conluio(neo4j_teste, Escopo.licitacao("087|P"), grau_maximo=5))
    assert o.nivel == AUXILIAR
    assert o.classe == DESCONHECIDA  # declarado no certame: nunca "posterior"
    assert o.vinculo.contatoAmplo is True
    assert o.vinculo.grauContato == 5


@pytest.mark.parametrize(("grau", "nivel"), [(None, PRINCIPAL), (2, PRINCIPAL), (40, AUXILIAR)])
def test_endereco_declarado_usa_grau_nacional(g, neo4j_teste, grau, nivel):
    """Prédio comercial (muitos estabelecimentos no país) não sustenta alerta, mesmo declarado no certame."""
    par_simples(g)
    g.declarou_endereco(A, "60000123|100", "087|P", date(2020, 6, 1), grau)
    g.declarou_endereco(B, "60000123|100", "087|P", date(2020, 6, 1), grau)
    o = _uma(buscar_conluio(neo4j_teste, Escopo.licitacao("087|P"), grau_maximo=5))
    assert (o.vinculo.tipoVinculo, o.vinculo.fonte, o.nivel) == ("endereco", "tce_505", nivel)
    if nivel == AUXILIAR:
        assert o.classe == DESCONHECIDA


def test_contato_da_rfb_e_auxiliar_e_sem_numero_e_ignorado(g, neo4j_teste):
    par_simples(g)
    g.telefone_rfb(A, "8530000000").telefone_rfb(B, "8530000000")
    g.endereco_rfb(A, "end-sn", sem_numero=True).endereco_rfb(B, "end-sn", sem_numero=True)
    r = buscar_conluio(neo4j_teste, Escopo.licitacao("087|P"))
    o = _uma(r)
    assert [(v.tipoVinculo, v.nivel, v.fonte, v.confiancaTemporal) for v in o.vinculos] == [
        ("telefone", AUXILIAR, "rfb", DESCONHECIDA)
    ]
    assert r.contatosSemNumeroIgnorados == 1


def test_contato_da_rfb_com_grau_nacional_alto_e_marcado_amplo(g, neo4j_teste):
    par_simples(g)
    g.telefone_rfb(A, "8530000000", grau_nacional=40).telefone_rfb(B, "8530000000", grau_nacional=40)
    o = _uma(buscar_conluio(neo4j_teste, Escopo.licitacao("087|P")))
    assert (o.vinculo.contatoAmplo, o.vinculo.grauContato) == (True, 40)


def test_principal_e_auxiliar_do_mesmo_par_nao_se_somam(g, neo4j_teste):
    par_simples(g)
    g.socio("p", A[:8], date(2010, 1, 1)).socio("p", B[:8], date(2011, 1, 1))
    g.telefone_rfb(A, "8530000000").telefone_rfb(B, "8530000000")
    r = buscar_conluio(neo4j_teste, Escopo.licitacao("087|P"))
    o = _uma(r)
    assert o.classe == PRINCIPAL
    assert [v.nivel for v in o.vinculos] == [PRINCIPAL, AUXILIAR]
    assert r.contagens() == {
        "ocorrenciasPrincipal": 1,
        "ocorrenciasConfiancaDesconhecida": 0,
        "ocorrenciasVinculoPosterior": 0,
        "ocorrenciasAproximado": 0,
        "ocorrenciasPista": 0,
        "paresPrincipal": 1,
        "paresSoNoAuxiliar": 0,
        "paresSoNoAproximado": 0,
        "paresSoNaPista": 0,
    }


# Recortes e cobertura ---------------------------------------------------------------------------
def test_filiais_da_mesma_empresa_nao_formam_par(g, neo4j_teste):
    par_simples(g)
    filial = A[:8] + "000299"
    g.empresa(filial).participa(filial, "087|P", date(2020, 6, 1))
    r = buscar_conluio(neo4j_teste, Escopo.licitacao("087|P"))
    assert r.ocorrencias == []
    assert r.paresMesmaRaiz == 1


def test_escopo_empresa_so_devolve_pares_da_empresa(g, neo4j_teste):
    itatira(g, antecipada=True)
    r = buscar_conluio(neo4j_teste, Escopo.empresa(C))
    assert [(o.cnpjA, o.cnpjB) for o in r.ocorrencias] == [(A, C)]


def test_escopo_municipio_respeita_o_periodo(g, neo4j_teste):
    itatira(g, antecipada=True)
    r = buscar_conluio(neo4j_teste, Escopo.municipio("087", date(2021, 1, 1), date(2021, 12, 31)))
    assert [o.licitacao for o in r.ocorrencias] == ["087|L2"]


def test_cobertura_por_municipio_e_ano(g, neo4j_teste):
    itatira(g)
    g.licitacao("087|SO1", date(2021, 3, 1)).participa(A, "087|SO1", date(2021, 3, 1))
    g.licitacao("087|DISPENSA", date(2021, 4, 1), de_fato=False)
    linhas = cobertura(neo4j_teste, "087")
    assert [
        (x["ano"], x["certamesDeFato"], x["com2OuMaisParticipantes"], x["proporcaoCom2OuMais"])
        for x in linhas
    ] == [(2019, 1, 1, 1.0), (2021, 2, 1, 0.5)]
