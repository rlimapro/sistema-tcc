"""Auditoria do Cypher livre contra o Neo4j de teste: registro feito pelo caminho real da tool e
reexecução que confere (ou não) o hash (D-44)."""

from __future__ import annotations

import pytest

from sarc.grafo.conexao import escrever, ler
from sarc.mcp_server import auditoria, ferramentas
from sarc.mcp_server.auditoria import Auditoria
from sarc.mcp_server.servidor import _protegido

pytestmark = pytest.mark.integration

CONSULTA = "MATCH (m:Municipio) RETURN m.nome AS nome ORDER BY nome"


def _auditar(aud, driver, consulta, limite=None):
    return aud.auditar(
        consulta, limite, "p", lambda: ferramentas.cypher_somente_leitura(driver, consulta, limite)
    )


def test_registro_reexecutado_confere_e_depois_difere(neo4j_teste, tmp_path):
    escrever(neo4j_teste, "UNWIND ['Itatira', 'Iguatu'] AS n CREATE (:Municipio {nome: n})")
    aud = Auditoria(tmp_path)
    _auditar(aud, neo4j_teste, CONSULTA)
    [registro] = auditoria.ler_registros(auditoria.arquivos_de(tmp_path))
    assert registro["status"] == "ok" and registro["nLinhas"] == 2
    assert registro["carga"] is not None  # regime lido da resposta, sem consulta extra

    assert auditoria.verificar(neo4j_teste, registro).situacao == auditoria.BATE

    escrever(neo4j_teste, "CREATE (:Municipio {nome: 'Crato'})")
    v = auditoria.verificar(neo4j_teste, registro)
    assert v.situacao == auditoria.DIFERE
    assert v.soNaReexecucao == [{"nome": "Crato"}] and v.soNoRegistro == []


def test_escrita_recusada_pelo_banco_e_registrada_como_recusa(neo4j_teste, tmp_path):
    aud = Auditoria(tmp_path)
    consulta = "CREATE (n:Intrusa) RETURN n"
    # mesmo encadeamento do servidor: a auditoria envolve o tratamento de erro da tool
    r = aud.auditar(
        consulta,
        None,
        None,
        lambda: _protegido(lambda: ferramentas.cypher_somente_leitura(neo4j_teste, consulta, None)),
    )
    assert r["erro"] == "escrita_recusada_pelo_banco"
    [registro] = auditoria.ler_registros(auditoria.arquivos_de(tmp_path))
    assert registro["status"] == "recusada" and registro["erro"]["codigo"].endswith("AccessMode")


def test_tokens_de_contato_nao_impedem_a_conferencia(neo4j_teste, tmp_path, monkeypatch):
    escrever(neo4j_teste, "CREATE (:Telefone {numero: '85999990000'})")
    aud = Auditoria(tmp_path)
    _auditar(aud, neo4j_teste, "MATCH (t:Telefone) RETURN t")
    [registro] = auditoria.ler_registros(auditoria.arquivos_de(tmp_path))
    assert "85999990000" not in str(registro)
    monkeypatch.setattr("sarc.mcp_server.privacidade._SAL", b"o" * 32)
    assert auditoria.verificar(neo4j_teste, registro).situacao == auditoria.BATE_SEM_TOKENS


def test_tipos_do_neo4j_reexecutados_batem(neo4j_teste, tmp_path):
    escrever(
        neo4j_teste,
        "CREATE (:Contrato {id: 'c1', data: date('2022-03-01'), valor: 1234.56, "
        "momento: datetime('2022-03-01T10:00:00-03:00'), lista: [1.0, 2.5]})",
    )
    aud = Auditoria(tmp_path)
    consulta = (
        "MATCH (c:Contrato) RETURN c, c.data AS d, c.momento AS m, c.valor / 3 AS v, "
        "duration('P1M2D') AS dur, point({x: 1.5, y: 2.0}) AS p"
    )
    _auditar(aud, neo4j_teste, consulta)
    [registro] = auditoria.ler_registros(auditoria.arquivos_de(tmp_path))
    assert registro["status"] == "ok" and registro["linhas"][0]["d"] == "2022-03-01"
    assert auditoria.verificar(neo4j_teste, registro).situacao == auditoria.BATE


def test_registro_adulterado_com_escrita_nao_escreve_na_verificacao(neo4j_teste, tmp_path):
    registro = {
        "id": "adulterado",
        "status": "ok",
        "consulta": "CREATE (n:Intrusa) RETURN n",
        "limiteAplicado": 10,
        "linhas": [],
        "nLinhas": 0,
        "hashLinhas": auditoria.hash_de([]),  # adulterado com cuidado: íntegro, então é reexecutado
    }
    v = auditoria.verificar(neo4j_teste, registro)
    assert v.situacao == auditoria.FALHOU and not v.confere
    assert ler(neo4j_teste, "MATCH (n:Intrusa) RETURN count(n) AS n")[0]["n"] == 0
    registro["consulta"] = "LOAD CSV FROM 'http://localhost:1/x' AS l RETURN l"
    assert auditoria.verificar(neo4j_teste, registro).situacao == auditoria.FALHOU


def test_erro_do_banco_nao_vaza_o_dado_na_resposta_nem_no_registro(neo4j_teste, tmp_path, caplog):
    """Revisão de 02/11/2026: `date(t.numero)` faz o Neo4j repetir o telefone na mensagem de erro."""
    import json
    import logging

    from sarc.mcp_server.servidor import criar_servidor

    escrever(neo4j_teste, "CREATE (:Telefone {numero: '85999990000'}), (:Cep {cep: '60000-000'})")
    aud = Auditoria(tmp_path)
    servidor = criar_servidor(neo4j_teste, 5, aud)
    import asyncio

    respostas = []
    with caplog.at_level(logging.WARNING):
        for consulta in (
            "MATCH (t:Telefone) RETURN date(t.numero) AS d",
            "MATCH (c:Cep) RETURN date(c.cep) AS d",
        ):
            r = asyncio.run(servidor.call_tool("cypher_somente_leitura", {"consulta": consulta}))
            respostas.append(json.loads(r.content[0].text))
    registros = list(auditoria.ler_registros(auditoria.arquivos_de(tmp_path)))
    for r, reg in zip(respostas, registros, strict=True):
        assert r["erro"] == "erro_do_banco" and r["codigo"].startswith("Neo.ClientError.Statement.")
        assert "Text cannot be parsed to a Date" in r["mensagem"]
        assert reg["status"] == "erro" and reg["erro"]["mensagem"] == r["mensagem"]
    tudo = json.dumps(respostas) + json.dumps(registros) + caplog.text
    assert "85999990000" not in tudo and "60000-000" not in tudo
