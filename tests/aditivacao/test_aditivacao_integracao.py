"""Consulta de aditivação contra o Neo4j de teste, com os contratos reais da fixture gravados no teste."""

from __future__ import annotations

from datetime import date

import pytest
from neo4j.exceptions import ClientError

from sarc.aditivacao.consultas import (
    Escopo,
    aditivacao_do_contrato,
    aditivacao_do_fornecedor,
    buscar_aditivacao,
    contratos_com_aditivacao,
    distribuicao_aditivacao,
)
from sarc.aditivacao.regras import (
    AUXILIAR,
    DENTRO,
    EXCLUIDO,
    MOTIVO_ACRESCIMO_NAO_QUANTIFICADO,
    MOTIVO_SO_NA_AMPLA,
    PRINCIPAL,
)
from sarc.grafo.conexao import escrever, ler
from tests.aditivacao import dados

pytestmark = pytest.mark.integration

ESPERADO = {
    dados.MORADA_NOVA: (PRINCIPAL, None),
    dados.MORRINHOS: (PRINCIPAL, None),
    dados.ACOPIARA: (AUXILIAR, MOTIVO_SO_NA_AMPLA),
    dados.ICO: (EXCLUIDO, MOTIVO_ACRESCIMO_NAO_QUANTIFICADO),
    dados.QUIXADA_001: (PRINCIPAL, None),
    dados.QUIXADA_002: (PRINCIPAL, None),
    dados.BANABUIU_NEGATIVO: (DENTRO, None),
    dados.CARNAUBAL_REFORMA_ESCOLAR: (DENTRO, None),
}


@pytest.fixture
def grafo(neo4j_teste):
    dados.gravar_fixture(neo4j_teste)
    return neo4j_teste


@pytest.mark.parametrize(("contrato", "esperado"), list(ESPERADO.items()))
def test_cada_contrato_da_fixture_tem_o_nivel_esperado(grafo, contrato, esperado):
    resultado = buscar_aditivacao(grafo, Escopo.contrato(contrato))
    assert len(resultado.avaliacoes) == 1
    a = resultado.avaliacoes[0]
    assert (a.nivel, a.motivo) == esperado
    assert isinstance(a.dataContrato, date)


def test_aditiva_so_chega_a_original_e_nao_duplica_ao_regravar(grafo):
    dados.gravar_fixture(grafo)
    linhas = ler(
        grafo,
        "MATCH (a:Contrato)-[r:ADITIVA]->(o:Contrato) "
        "RETURN count(r) AS arestas, count(DISTINCT a) AS aditivos, "
        "sum(CASE WHEN o.classe <> 'original' OR a.classe = 'original' THEN 1 ELSE 0 END) AS erradas",
    )[0]
    assert linhas == {"arestas": 50, "aditivos": 50, "erradas": 0}


def test_escopo_de_municipio_separa_as_listas_e_conta_orfaos(grafo):
    escrever(
        grafo,
        "CREATE (:Contrato {id: '113|X|2020-01-01#Y|2019-01-01', municipio: '113', classe: 'acrescimo', "
        "modalidade: 'AA', dataContrato: date('2020-01-01'), idOriginal: '113|Y|2019-01-01'}), "
        "(:Contrato {id: '113|Z|2020-01-01', municipio: '113', classe: 'acrescimo', modalidade: 'AA', "
        "dataContrato: date('2020-01-01')})",
    )
    resultado = buscar_aditivacao(grafo, Escopo.municipio("113"))
    assert [a.id for a in resultado.alertas()] == [dados.MORADA_NOVA]
    assert resultado.contagens()["aditivosOrfaos"] == 1
    assert resultado.contagens()["aditivosSemNumeroOriginal"] == 1


def test_so_na_ampla_so_aparece_sob_pedido(grafo):
    resultado = buscar_aditivacao(grafo, Escopo.municipio("005"))
    assert resultado.alertas_a_revisar() == []
    assert [a.id for a in resultado.alertas_a_revisar(incluir_so_na_ampla=True)] == [dados.ACOPIARA]
    assert resultado.contagens()["contratosAuxiliarPorMotivo"] == {MOTIVO_SO_NA_AMPLA: 1}


def test_alertas_em_ordem_de_excesso_em_reais(grafo):
    resultado = buscar_aditivacao(grafo, Escopo.municipio("144", date(2021, 1, 1), date(2021, 12, 31)))
    excessos = [a.excessoReais for a in resultado.alertas()]
    assert [a.id for a in resultado.alertas()] == [dados.QUIXADA_002, dados.QUIXADA_001]
    assert excessos == sorted(excessos, reverse=True)


def test_recorte_de_datas_pelo_original(grafo):
    assert buscar_aditivacao(grafo, Escopo.municipio("144", date(2022, 1, 1))).avaliacoes == []


def test_fornecedor_e_licitacao_de_origem_na_saida(grafo):
    dados.fornecedor(grafo, "07270402000155", dados.MORRINHOS, "LIMPAX")
    escrever(
        grafo,
        "MATCH (c:Contrato {id: $c}) MERGE (l:Licitacao {id: '115|X'}) MERGE (c)-[:ORIGINADO_DE]->(l)",
        {"c": dados.MORRINHOS},
    )
    a = buscar_aditivacao(grafo, Escopo.contrato(dados.MORRINHOS)).avaliacoes[0]
    assert a.fornecedores == [{"cnpj": "07270402000155", "razaoSocial": "LIMPAX"}]
    assert a.licitacaoOrigem == "115|X"
    por_fornecedor = buscar_aditivacao(grafo, Escopo.fornecedor("07270402000155"))
    assert [x.id for x in por_fornecedor.avaliacoes] == [dados.MORRINHOS]


def test_contrato_sem_aditivo_de_valor_aparece_no_escopo_de_contrato_e_nao_no_de_municipio(grafo):
    escrever(
        grafo,
        "CREATE (:Contrato {id: '113|S|2020-01-01', municipio: '113', classe: 'original', modalidade: 'OR', "
        "dataContrato: date('2020-01-01'), valorTotal: 10.0, valorEspurio: false, valorDivergente: false})",
    )
    assert buscar_aditivacao(grafo, Escopo.contrato("113|S|2020-01-01")).avaliacoes[0].nivel == DENTRO
    ids = {a.id for a in buscar_aditivacao(grafo, Escopo.municipio("113")).avaliacoes}
    assert "113|S|2020-01-01" not in ids


def test_aditivo_nao_e_avaliado_como_contrato(grafo):
    aditivo = next(i for i, linha in dados.contratos().items() if linha["idOriginal"] == dados.MORADA_NOVA)
    assert buscar_aditivacao(grafo, Escopo.contrato(aditivo)).avaliacoes == []


def test_texto_dos_aditivos_so_trafega_quando_pode_estourar(grafo):
    escrever(
        grafo,
        "CREATE (o:Contrato {id: '113|P|2020-01-01', municipio: '113', classe: 'original', modalidade: 'OR', "
        "dataContrato: date('2020-01-01'), valorTotal: 1000.0, valorEspurio: false, valorDivergente: false}) "
        "CREATE (:Contrato {id: '113|P1|2020-02-01#P|2020-01-01', municipio: '113', classe: 'acrescimo', "
        "modalidade: 'AA', dataContrato: date('2020-02-01'), valorTotal: 100.0, valorEspurio: false, "
        "valorDivergente: false, objeto: 'texto'})-[:ADITIVA]->(o)",
    )
    pequeno = next(
        a for a in buscar_aditivacao(grafo, Escopo.municipio("113")).avaliacoes if a.id == "113|P|2020-01-01"
    )
    assert pequeno.nivel == DENTRO and pequeno.aditivos[0].objeto is None
    sozinho = buscar_aditivacao(grafo, Escopo.contrato("113|P|2020-01-01")).avaliacoes[0]
    assert sozinho.aditivos[0].objeto == "texto"


def test_consulta_roda_em_sessao_de_leitura(grafo):
    # A consulta é só leitura; a mesma sessão recusa escrita (garantia do servidor, D-14).
    with pytest.raises(ClientError):
        ler(grafo, "CREATE (:Teste)")
    assert buscar_aditivacao(grafo, Escopo.municipio("115")).avaliacoes


# API para a tool MCP ---------------------------------------------------------------------------
def test_aditivacao_do_contrato_devolve_dicionario_com_aditivos_qualificados(grafo):
    saida = aditivacao_do_contrato(grafo, dados.ACOPIARA)
    contrato = saida["contrato"]
    assert (contrato["nivel"], contrato["motivo"]) == (AUXILIAR, MOTIVO_SO_NA_AMPLA)
    aditivo = contrato["aditivos"][0]
    assert aditivo["estendeVigencia"] is True and aditivo["fracaoDoOriginal"] == pytest.approx(2.0)
    assert aditivacao_do_contrato(grafo, "nao|existe|2020-01-01") is None


def test_contratos_com_aditivacao_separa_listas_e_corta_sem_mudar_contagens(grafo):
    saida = contratos_com_aditivacao(grafo, "144", None, None, limite=1)
    assert [a["id"] for a in saida["alertas"]] == [dados.QUIXADA_002]
    assert saida["listasTruncadas"] is True
    assert saida["contagens"]["contratosPrincipal"] == 2
    assert "total" not in " ".join(saida["contagens"]).lower()
    com_ampla = contratos_com_aditivacao(grafo, "005", None, None, limite=10, incluir_so_na_ampla=True)
    assert [a["id"] for a in com_ampla["alertasARevisar"]] == [dados.ACOPIARA]
    assert contratos_com_aditivacao(grafo, "005", None, None, limite=10)["alertasARevisar"] == []


def test_aditivacao_do_fornecedor(grafo):
    dados.fornecedor(grafo, "07270402000155", dados.MORRINHOS, "LIMPAX")
    saida = aditivacao_do_fornecedor(grafo, "07270402000155", None, None, limite=10)
    assert [a["id"] for a in saida["alertas"]] == [dados.MORRINHOS]


# Distribuição para a monografia ------------------------------------------------------------------
def test_distribuicao_do_banco_inteiro_por_ano_e_municipio(grafo):
    saida = distribuicao_aditivacao(grafo)
    por_ano = {(x["ano"], x["nivel"], x["motivo"]): x for x in saida["porAno"]}
    assert por_ano[(2021, PRINCIPAL, None)]["contratos"] == 2  # Quixadá 001 e 002
    assert por_ano[(2013, AUXILIAR, MOTIVO_SO_NA_AMPLA)]["contratos"] == 1  # Acopiara
    assert por_ano[(2019, EXCLUIDO, MOTIVO_ACRESCIMO_NAO_QUANTIFICADO)]["contratos"] == 1  # Icó
    assert por_ano[(2019, PRINCIPAL, None)]["excessoReais"] == pytest.approx(
        1268800.81 - 0.25 * 1938028.53, abs=0.01
    )
    por_municipio = {(x["municipio"], x["nivel"]): x["contratos"] for x in saida["porMunicipio"]}
    assert por_municipio[("144", PRINCIPAL)] == 2 and por_municipio[("115", PRINCIPAL)] == 1
    assert sum(x["contratos"] for x in saida["porAno"]) == len(ESPERADO)
    assert saida["escopo"]["municipios"] == len({c.split("|")[0] for c in ESPERADO})
    assert saida["contagens"]["contratosPrincipal"] == 4
    assert saida["contagens"]["contratosAuxiliarPorMotivo"] == {MOTIVO_SO_NA_AMPLA: 1}
    assert saida["contagens"]["aditivosOrfaos"] == 0


def test_distribuicao_de_um_municipio_traz_os_orfaos(grafo):
    saida = distribuicao_aditivacao(grafo, "144", date(2021, 1, 1), date(2021, 12, 31))
    assert {x["municipio"] for x in saida["porMunicipio"]} == {"144"}
    assert saida["contagens"]["aditivosOrfaos"] == 0
