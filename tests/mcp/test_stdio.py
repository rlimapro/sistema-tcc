"""Servidor MCP de ponta a ponta: um cliente MCP real sobe `sarc mcp` por stdio, lista as tools e
chama uma tool de cada indício contra o Neo4j de teste (herdado de `mvp-v2/tests/test_mcp_integration.py`).
"""

from __future__ import annotations

import asyncio
import json
import os
import sys

import pytest

from sarc.grafo.conexao import escrever
from tests.aditivacao import dados as aditivacao
from tests.conluio.grafo import Grafo, itatira
from tests.pretericao.dados import CREDOR_B, cenario_dois_componentes, gravar

pytestmark = pytest.mark.integration

TOOLS = {
    "conluio_na_licitacao",
    "aditivacao_do_contrato",
    "contratos_com_aditivacao",
    "aditivacao_do_fornecedor",
    "ranking_de_preteridos",
    "pretericao_do_credor",
    "municipios_com_pretericao",
    "licitacoes_com_conluio",
    "vinculos_da_empresa",
    "cobertura_dos_dados",
    "resumo_da_carga",
    "esquema_do_grafo",
    "cypher_somente_leitura",
    "recusar_pergunta",
}


def _conversar(env_file: str) -> dict:
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    async def conversar() -> dict:
        params = StdioServerParameters(
            command=sys.executable,
            args=["-c", "from sarc.cli import main; main()", "--env-file", env_file, "mcp"],
            env={**os.environ},
            # cwd `/` como num cliente MCP qualquer: nada pode depender da pasta de trabalho (D-44)
            cwd="/",
        )
        saida: dict = {}
        async with stdio_client(params) as (leitura, escrita), ClientSession(leitura, escrita) as sessao:
            inicial = await sessao.initialize()
            saida["servidor"] = inicial.server_info.name
            ferramentas = (await sessao.list_tools()).tools
            saida["tools"] = {f.name: f for f in ferramentas}

            async def chamar(nome: str, argumentos: dict) -> dict:
                r = await sessao.call_tool(nome, argumentos)
                return json.loads(r.content[0].text)

            saida["conluio"] = await chamar("conluio_na_licitacao", {"licitacao": "087|L2"})
            saida["aditivacao"] = await chamar("contratos_com_aditivacao", {"municipio": "Morada Nova"})
            saida["contrato"] = await chamar("aditivacao_do_contrato", {"contrato": "999|NADA|2020-01-01"})
            saida["preterição"] = await chamar("ranking_de_preteridos", {"municipio": "Iguatu"})
            saida["naoCalculada"] = await chamar("ranking_de_preteridos", {"municipio": "Itatira"})
            saida["senha"] = await chamar(
                "cypher_somente_leitura",
                {"consulta": "ALTER CURRENT USER SET PASSWORD FROM 'sarc-teste-local' TO 'outra-senha-123'"},
            )
            saida["livre"] = await chamar(
                "cypher_somente_leitura",
                {
                    "consulta": "MATCH (m:Municipio) RETURN m.codigo AS codigo ORDER BY codigo",
                    "pergunta": "quais?",
                },
            )
            saida["recusa"] = await chamar(
                "recusar_pergunta", {"tipo": "afirmacao_nao_sustentada", "pergunta": "a empresa fraudou?"}
            )
        return saida

    return asyncio.run(conversar())


@pytest.fixture
def respostas(neo4j_teste, tmp_path):
    itatira(Grafo(neo4j_teste), antecipada=True)
    aditivacao.gravar_fixture(neo4j_teste)
    escrever(neo4j_teste, "MERGE (:Municipio {codigo: '113', nome: 'Morada Nova'})")
    gravar(neo4j_teste, "074", "2025+0", cenario_dois_componentes())
    escrever(neo4j_teste, "MATCH (m:Municipio {codigo: '074'}) SET m.nome = 'Iguatu'")
    uri = os.environ.get("NEO4J_TEST_URI", "bolt://localhost:7700")
    senha = os.environ.get("NEO4J_TEST_PASSWORD", "sarc-teste-local")
    env = tmp_path / "teste.env"
    env.write_text(
        f"NEO4J_URI={uri}\nNEO4J_USER=neo4j\nNEO4J_PASSWORD={senha}\nLOG_LEVEL=WARNING\n"
        f"AUDITORIA_DIR={tmp_path / 'auditoria'}\n"
    )
    saida = _conversar(str(env))
    saida["auditoria"] = [
        json.loads(linha)
        for f in sorted((tmp_path / "auditoria").glob("cypher-*.jsonl"))
        for linha in f.open()
    ]
    saida["chamadas"] = [
        json.loads(linha)
        for f in sorted((tmp_path / "auditoria").glob("chamadas-*.jsonl"))
        for linha in f.open()
    ]
    return saida


def test_cliente_mcp_real_lista_as_tools_e_chama_conluio(respostas):
    assert respostas["servidor"] == "sarc-ce"
    assert set(respostas["tools"]) == TOOLS
    assert all(t.annotations and t.annotations.read_only_hint for t in respostas["tools"].values())
    d = respostas["conluio"]["dados"]
    assert [a["tipoVinculo"] for a in d["alertas"]] == ["cadeia_societaria"]
    assert respostas["conluio"]["consultaExecutada"]
    assert respostas["conluio"]["limiteDeLeitura"]


def test_cliente_mcp_real_chama_aditivacao(respostas):
    d = respostas["aditivacao"]["dados"]
    assert [a["id"] for a in d["alertas"]] == [aditivacao.MORADA_NOVA]
    assert respostas["aditivacao"]["garantiasDeMetodo"]["semTotalSomado"]
    assert respostas["contrato"]["erro"] == "parametro_invalido"
    descricao = respostas["tools"]["contratos_com_aditivacao"].description
    assert "incluir_so_na_ampla" in descricao and "teto" in descricao


def test_cliente_mcp_real_chama_pretericao(respostas):
    d = respostas["preterição"]["dados"]
    assert d["calculado"] is True
    assert d["rankingPrincipal"][0]["cnpj"] == CREDOR_B
    assert "listaAuxiliarNaoPagas" in d
    assert respostas["naoCalculada"]["dados"]["calculado"] is False


def test_troca_de_senha_pelo_cliente_e_recusada(respostas):
    assert respostas["senha"]["erro"] == "escrita_recusada_pelo_banco"


def test_recusa_chega_como_recusa(respostas):
    assert respostas["recusa"]["recusa"] == "afirmacao_nao_sustentada"
    assert respostas["recusa"]["respostaVazia"] is False


def test_cypher_livre_pelo_cliente_fica_na_auditoria(respostas):
    senha, livre = respostas["auditoria"]
    assert senha["status"] == "recusada" and senha["pergunta"] is None
    assert senha["erro"]["erro"] == "escrita_recusada_pelo_banco"
    assert livre["status"] == "ok" and livre["pergunta"] == "quais?"
    assert livre["linhas"] == respostas["livre"]["dados"]["linhas"]
    assert senha["sessao"] == livre["sessao"]


def test_toda_chamada_pelo_cliente_fica_na_trilha(respostas):
    trilha = respostas["chamadas"]
    assert [c["tool"] for c in trilha] == [
        "conluio_na_licitacao",
        "contratos_com_aditivacao",
        "aditivacao_do_contrato",
        "ranking_de_preteridos",
        "ranking_de_preteridos",
        "cypher_somente_leitura",
        "cypher_somente_leitura",
        "recusar_pergunta",
    ]
    assert trilha[2]["status"] == "erro" and trilha[2]["erro"] == "parametro_invalido"
    assert trilha[6]["pergunta"] == "quais?"
    assert [c["registroCypher"] for c in trilha[5:7]] == [r["id"] for r in respostas["auditoria"]]
    assert len({c["sessao"] for c in trilha} | {r["sessao"] for r in respostas["auditoria"]}) == 1
