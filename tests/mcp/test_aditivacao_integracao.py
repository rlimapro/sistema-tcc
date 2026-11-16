"""Tools de aditivação do MCP contra o Neo4j de teste, com os contratos reais da fixture de aditivação:
envelope, listas separadas sem total (F-05), tetos (F-18), erros legíveis e leitura em READ_ACCESS (F-04)."""

from __future__ import annotations

import json

import pytest
from neo4j import READ_ACCESS

from sarc.aditivacao.consultas import GARANTIAS_DE_METODO
from sarc.grafo.conexao import escrever
from sarc.mcp_server import ferramentas
from tests.aditivacao import dados

pytestmark = pytest.mark.integration

FORNECEDOR = "33333333000103"
MUNICIPIOS = {"113": "Morada Nova", "144": "Quixadá", "005": "Acopiara"}


@pytest.fixture
def grafo(neo4j_teste):
    dados.gravar_fixture(neo4j_teste)
    for codigo, nome in MUNICIPIOS.items():
        escrever(neo4j_teste, "MERGE (m:Municipio {codigo: $c}) SET m.nome = $n", {"c": codigo, "n": nome})
    return neo4j_teste


def _erro_ou_resposta(funcao, *args):
    try:
        return funcao(*args)
    except Exception as erro:  # noqa: BLE001
        return ferramentas.erro_para_resposta(erro)


def _sem_total(valor) -> bool:
    if isinstance(valor, dict):
        return not any("total" in str(k).lower() for k in valor) and all(
            _sem_total(v) for v in valor.values()
        )
    if isinstance(valor, list):
        return all(_sem_total(v) for v in valor)
    return True


# aditivacao_do_contrato -------------------------------------------------------------------------
def test_contrato_devolve_veredito_no_envelope(grafo):
    r = ferramentas.aditivacao_do_contrato(grafo, dados.MORADA_NOVA)
    d = r["dados"]
    assert d["id"] == dados.MORADA_NOVA
    assert d["nivel"] == "principal"
    assert d["aditivos"] and all("estendeVigencia" in a and "fracaoDoOriginal" in a for a in d["aditivos"])
    assert r["garantiasDeMetodo"] == GARANTIAS_DE_METODO
    assert "ADITIVA" in r["consultaExecutada"]
    assert r["limiteDeLeitura"].startswith("Indício, não acusação")
    assert r["regime"]["parametros"] == {"contrato": dados.MORADA_NOVA}
    json.dumps(r)  # datas já convertidas


def test_contrato_inexistente_vira_erro_legivel(grafo):
    r = _erro_ou_resposta(ferramentas.aditivacao_do_contrato, grafo, "999|NADA|2020-01-01")
    assert r["erro"] == "parametro_invalido"
    assert "não encontrado" in r["mensagem"]


def test_id_de_aditivo_aponta_o_original(grafo):
    aditivo = next(i for i, linha in dados.contratos().items() if linha["idOriginal"] == dados.MORADA_NOVA)
    r = _erro_ou_resposta(ferramentas.aditivacao_do_contrato, grafo, aditivo)
    assert r["erro"] == "parametro_invalido"
    assert repr(dados.MORADA_NOVA) in r["mensagem"]


def test_contrato_vazio_vira_erro(grafo):
    assert _erro_ou_resposta(ferramentas.aditivacao_do_contrato, grafo, "  ")["erro"] == "parametro_invalido"


# contratos_com_aditivacao -----------------------------------------------------------------------
def test_municipio_por_nome_separa_listas_e_nao_soma(grafo):
    r = ferramentas.contratos_com_aditivacao(grafo, "morada nova", None, None, None)
    d = r["dados"]
    assert d["municipio"]["codigo"] == "113"
    assert [a["id"] for a in d["alertas"]] == [dados.MORADA_NOVA]
    assert d["alertasARevisar"] == []
    assert d["contagens"]["contratosPrincipal"] == 1
    assert "aditivosOrfaos" in d["contagens"]
    assert _sem_total(d)
    assert r["regime"]["parametros"]["municipio"] == "113"
    assert r["regime"]["parametros"]["incluirSoNaAmpla"] is False


def test_limite_corta_listas_mas_nao_as_contagens(grafo):
    r = ferramentas.contratos_com_aditivacao(grafo, "144", None, None, 1)
    d = r["dados"]
    assert len(d["alertas"]) == 1
    assert d["listasCortadasEm"] == 1
    assert d["listasTruncadas"] is True
    assert d["contagens"]["contratosPrincipal"] == 2
    assert "CORTADAS" in r["limiteDeLeitura"]


def test_limite_tem_teto(grafo):
    r = ferramentas.contratos_com_aditivacao(grafo, "144", None, None, 10_000)
    assert r["dados"]["listasCortadasEm"] == ferramentas.LIMITE_MAXIMO
    assert r["dados"]["listasTruncadas"] is False
    assert r["dados"]["motivoDoCorte"] is None


def test_so_na_ampla_so_contado_por_padrao(grafo):
    padrao = ferramentas.contratos_com_aditivacao(grafo, "Acopiara", None, None, None)
    assert padrao["dados"]["alertasARevisar"] == []
    assert padrao["dados"]["contagens"]["contratosAuxiliarPorMotivo"] == {"so_na_ampla": 1}
    assert "incluir_so_na_ampla" in padrao["limiteDeLeitura"]
    listado = ferramentas.contratos_com_aditivacao(grafo, "Acopiara", None, None, None, True)
    assert [a["id"] for a in listado["dados"]["alertasARevisar"]] == [dados.ACOPIARA]
    assert "incluir_so_na_ampla" not in listado["limiteDeLeitura"]


def test_recorte_de_datas(grafo):
    r = ferramentas.contratos_com_aditivacao(grafo, "144", "2022", None, None)
    assert r["dados"]["alertas"] == []
    assert r["regime"]["parametros"]["desde"] == "2022-01-01"
    r = ferramentas.contratos_com_aditivacao(grafo, "144", "2021", "2021-12-31", None)
    assert len(r["dados"]["alertas"]) == 2


@pytest.mark.parametrize(
    ("municipio", "desde"),
    [("Município Inexistente", None), ("", None), ("144", "07/04/2021")],
)
def test_entrada_invalida_vira_erro_legivel(grafo, municipio, desde):
    r = _erro_ou_resposta(ferramentas.contratos_com_aditivacao, grafo, municipio, desde, None, None)
    assert r["erro"] == "parametro_invalido"


def test_teto_de_caracteres_corta_as_listas(grafo, monkeypatch):
    monkeypatch.setattr(ferramentas, "TETO_CARACTERES", 3_000)
    r = ferramentas.contratos_com_aditivacao(grafo, "144", None, None, None)
    d = r["dados"]
    assert len(json.dumps({k: d[k] for k in ("alertas", "alertasARevisar")})) <= 3_000
    assert d["listasTruncadas"] is True
    assert "caracteres" in d["motivoDoCorte"]
    assert d["contagens"]["contratosPrincipal"] == 2


# aditivacao_do_fornecedor -----------------------------------------------------------------------
def test_fornecedor_traz_denominador_com_contratos_sem_aditivo(grafo):
    dados.fornecedor(grafo, FORNECEDOR, dados.MORADA_NOVA, "Construtora Teste")
    escrever(
        grafo,
        "CREATE (:Contrato {id: '113|SEM|2020-02-02', municipio: '113', classe: 'original', "
        "modalidade: 'OR', valorTotal: 1000.0, dataContrato: date('2020-02-02')})",
    )
    dados.fornecedor(grafo, FORNECEDOR, "113|SEM|2020-02-02", "Construtora Teste")
    r = ferramentas.aditivacao_do_fornecedor(grafo, "33.333.333/0001-03", None, None, None)
    d = r["dados"]
    assert d["cnpj"] == FORNECEDOR
    assert d["contratosOriginaisDoFornecedor"] == 2
    assert [a["id"] for a in d["alertas"]] == [dados.MORADA_NOVA]
    assert d["alertas"][0]["fornecedores"][0]["cnpj"] == FORNECEDOR
    assert _sem_total(d)
    assert "CONTRATADO_EM" in r["consultaExecutada"]


@pytest.mark.parametrize("cnpj", ["123", "44444444000104"])
def test_fornecedor_invalido_ou_ausente_vira_erro(grafo, cnpj):
    r = _erro_ou_resposta(ferramentas.aditivacao_do_fornecedor, grafo, cnpj, None, None, None)
    assert r["erro"] == "parametro_invalido"


# F-04: toda leitura em READ_ACCESS ---------------------------------------------------------------
def test_tools_de_aditivacao_so_abrem_sessao_de_leitura(grafo, monkeypatch):
    dados.fornecedor(grafo, FORNECEDOR, dados.MORADA_NOVA, "Construtora Teste")
    modos: list[str | None] = []
    original = type(grafo).session

    def espiao(self, **kwargs):
        modos.append(kwargs.get("default_access_mode"))
        return original(self, **kwargs)

    monkeypatch.setattr(type(grafo), "session", espiao)
    ferramentas.aditivacao_do_contrato(grafo, dados.MORADA_NOVA)
    ferramentas.contratos_com_aditivacao(grafo, "113", None, None, None)
    ferramentas.aditivacao_do_fornecedor(grafo, FORNECEDOR, None, None, None)
    assert modos and all(m == READ_ACCESS for m in modos)
