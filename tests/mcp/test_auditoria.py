"""Auditoria do Cypher livre (D-44), sem banco: formato do registro, acréscimo, recusas, falha de
gravação tolerada, campo `pergunta`, comparação de hash e o subcomando `sarc auditoria`."""

from __future__ import annotations

import asyncio
import json
import logging
import traceback

import pytest
from click.testing import CliRunner

from sarc.cli import grupo
from sarc.mcp_server import auditoria, ferramentas
from sarc.mcp_server.auditoria import Auditoria
from sarc.mcp_server.servidor import criar_servidor

CARGAS = {"ultimaCarga": "2026-11-01T10:00:00", "totalCargas": 3, "snapshotsRfb": ["2026-10"]}


def resposta_ok(linhas, truncado=False, cargas=CARGAS):
    return {
        "dados": {"linhas": linhas, "nLinhas": len(linhas), "truncado": truncado, "motivoDoCorte": None},
        "consultaExecutada": "x",
        "regime": {"cargas": cargas},
    }


def ler(caminho):
    return [json.loads(x) for x in caminho.read_text(encoding="utf-8").splitlines()]


@pytest.fixture
def aud(tmp_path):
    return Auditoria(tmp_path / "aud", sessao="S1", banco="bolt://x")


def unico_arquivo(aud):
    arquivos = auditoria.arquivos_de(aud.diretorio)
    assert len(arquivos) == 1
    return arquivos[0]


class TestGravador:
    def test_registro_ok_tem_os_campos_e_o_hash(self, aud):
        linhas = [{"municipio": "Itatira", "n": 3}, {"municipio": "Iguatu", "n": 1}]
        r = aud.auditar("MATCH (n) RETURN n", 5, "top 5 municípios?", lambda: resposta_ok(linhas))
        assert r["dados"]["linhas"] == linhas  # a resposta passa intacta
        [reg] = ler(unico_arquivo(aud))
        assert unico_arquivo(aud).name.startswith("cypher-") and unico_arquivo(aud).suffix == ".jsonl"
        assert (
            reg["sessao"] == "S1" and reg["banco"] == "bolt://x" and reg["tool"] == "cypher_somente_leitura"
        )
        assert reg["status"] == "ok" and reg["motivo"] is None
        assert reg["pergunta"] == "top 5 municípios?"
        assert reg["consulta"] == "MATCH (n) RETURN n"
        assert (reg["limitePedido"], reg["limiteAplicado"]) == (5, 5)
        assert reg["nLinhas"] == 2 and reg["truncado"] is False
        assert reg["linhas"] == linhas
        assert reg["hashLinhas"] == auditoria.hash_de(linhas)
        assert reg["carga"]["ultimaCarga"] == "2026-11-01T10:00:00"
        assert reg["carga"]["hash"] == auditoria.hash_de(CARGAS)
        assert reg["duracaoMs"] >= 0
        assert "T" in reg["momento"] and ("+" in reg["momento"][-6:] or "-" in reg["momento"][-6:])
        assert len(reg["id"]) == 32

    def test_acrescenta_uma_linha_por_chamada_com_ids_distintos(self, aud):
        for i in range(3):
            aud.auditar(f"RETURN {i} AS i", None, None, lambda i=i: resposta_ok([{"i": i}]))
        registros = ler(unico_arquivo(aud))
        assert [r["consulta"] for r in registros] == ["RETURN 0 AS i", "RETURN 1 AS i", "RETURN 2 AS i"]
        assert len({r["id"] for r in registros}) == 3
        assert {r["sessao"] for r in registros} == {"S1"}
        assert registros[0]["limiteAplicado"] == ferramentas.LIMITE_MAXIMO

    def test_pergunta_ausente_fica_nula(self, aud):
        aud.auditar("RETURN 1", 10, None, lambda: resposta_ok([{"1": 1}]))
        assert ler(unico_arquivo(aud))[0]["pergunta"] is None

    @pytest.mark.parametrize(
        "consulta",
        ["MATCH (n) RETURN n // x", "LOAD CSV FROM 'http://x' AS l RETURN l", "CALL dbms.listConfig()"],
    )
    def test_recusa_do_filtro_e_registrada(self, aud, consulta):
        # caminho real da tool: o filtro recusa antes de tocar no driver
        r = aud.auditar(consulta, 10, "p", lambda: ferramentas.cypher_somente_leitura(None, consulta, 10))
        assert r["erro"] == "consulta_nao_permitida"
        [reg] = ler(unico_arquivo(aud))
        assert reg["status"] == "recusada" and reg["motivo"].startswith("não permitido")
        assert reg["erro"]["erro"] == "consulta_nao_permitida"
        assert reg["linhas"] is None and reg["hashLinhas"] is None

    def test_escrita_recusada_pelo_banco_e_recusa_e_tempo_esgotado_e_erro(self, aud):
        aud.auditar("CREATE (n)", 10, None, lambda: {"erro": "escrita_recusada_pelo_banco", "mensagem": "m"})
        aud.auditar(
            "MATCH p=()-[*]-() RETURN p", 10, None, lambda: {"erro": "tempo_esgotado", "mensagem": "t"}
        )
        registros = ler(unico_arquivo(aud))
        assert [r["status"] for r in registros] == ["recusada", "erro"]
        assert registros[1]["motivo"] == "t"

    def test_excecao_e_registrada_e_propagada(self, aud):
        def explode():
            raise RuntimeError("bum")

        with pytest.raises(RuntimeError):
            aud.auditar("RETURN 1", 10, None, explode)
        [reg] = ler(unico_arquivo(aud))
        assert reg["status"] == "erro" and reg["motivo"] == "RuntimeError: bum"

    def test_falha_de_gravacao_nao_derruba_a_consulta(self, tmp_path, caplog):
        ocupado = tmp_path / "arquivo-no-lugar-da-pasta"
        ocupado.write_text("x")
        aud = Auditoria(ocupado, sessao="S1")
        with caplog.at_level(logging.ERROR, logger="sarc.mcp_server.auditoria"):
            r = aud.auditar("RETURN 1", 10, None, lambda: resposta_ok([{"a": 1}]))
        assert r["dados"]["linhas"] == [{"a": 1}]
        assert "não gravou" in caplog.text

    def test_linha_ilegivel_e_pulada_na_leitura(self, aud):
        aud.auditar("RETURN 1", 10, None, lambda: resposta_ok([{"a": 1}]))
        with open(unico_arquivo(aud), "a", encoding="utf-8") as f:
            f.write("{quebrada\n")
        aud.auditar("RETURN 2", 10, None, lambda: resposta_ok([{"a": 2}]))
        assert len(list(auditoria.ler_registros([unico_arquivo(aud)]))) == 2

    def test_sessao_padrao_e_gerada(self, tmp_path):
        a, b = Auditoria(tmp_path), Auditoria(tmp_path)
        assert a.sessao != b.sessao and a.sessao[:8].isdigit()


class TestServidor:
    def chamar(self, servidor, argumentos):
        resultado = asyncio.run(servidor.call_tool("cypher_somente_leitura", argumentos))
        return json.loads(resultado.content[0].text)

    def test_tool_aceita_e_grava_pergunta_e_funciona_sem_ela(self, aud):
        servidor = criar_servidor(None, 5, aud)  # recusa pelo filtro não toca no driver
        r1 = self.chamar(servidor, {"consulta": "RETURN 1 // c"})
        r2 = self.chamar(servidor, {"consulta": "RETURN 2 // c", "pergunta": "quantos?", "limite": 7})
        assert r1["erro"] == r2["erro"] == "consulta_nao_permitida"
        registros = ler(unico_arquivo(aud))
        assert [r["pergunta"] for r in registros] == [None, "quantos?"]
        assert [r["limiteAplicado"] for r in registros] == [ferramentas.LIMITE_MAXIMO, 7]

    def test_descricao_e_instrucoes_pedem_a_pergunta(self, aud):
        servidor = criar_servidor(None, 5, aud)
        tools = {t.name: t for t in asyncio.run(servidor.list_tools())}
        tool = tools["cypher_somente_leitura"]
        assert "pergunta" in tool.description
        assert "pergunta" in tool.input_schema["properties"]
        assert tool.input_schema.get("required") == ["consulta"]
        from sarc.mcp_server.servidor import INSTRUCOES

        assert "`pergunta`" in INSTRUCOES


class TestComparacao:
    def registro(self, linhas, truncado=False, cargas=CARGAS):
        return {
            "id": "abc",
            "status": "ok",
            "consulta": "RETURN 1",
            "linhas": json.loads(json.dumps(linhas)),
            "nLinhas": len(linhas),
            "truncado": truncado,
            "hashLinhas": auditoria.hash_de(linhas),
            "carga": auditoria.impressao_da_carga(cargas),
        }

    def test_hash_canonico_ignora_ordem_das_chaves(self):
        assert auditoria.hash_de([{"a": 1, "b": "é"}]) == auditoria.hash_de([{"b": "é", "a": 1}])
        assert auditoria.hash_de([{"a": 1}]) != auditoria.hash_de([{"a": 2}])
        assert auditoria.hash_de([{"a": 1}]).startswith("sha256:")

    def test_bate(self):
        linhas = [{"m": "Itatira", "n": 3, "x": 1.5, "d": None}]
        v = auditoria.comparar(self.registro(linhas), resposta_ok(linhas))
        assert v.situacao == auditoria.BATE and v.confere and v.cargaMudou is False and not v.avisos

    def test_mesmas_linhas_em_outra_ordem(self):
        a, b = {"n": 1}, {"n": 2}
        v = auditoria.comparar(self.registro([a, b]), resposta_ok([b, a]))
        assert v.situacao == auditoria.BATE_FORA_DE_ORDEM and v.confere

    def test_so_tokens_de_privacidade_diferem(self):
        v = auditoria.comparar(
            self.registro([{"t": "contato:0123456789"}]), resposta_ok([{"t": "contato:abcdefabcd"}])
        )
        assert v.situacao == auditoria.BATE_SEM_TOKENS and v.confere

    def test_tokens_trocados_de_forma_consistente_em_outra_ordem_conferem(self):
        a, b, x, y = "contato:aaaaaaaaaa", "contato:bbbbbbbbbb", "contato:1111111111", "contato:2222222222"
        # mesmo dado (a->y, b->x), linhas em outra ordem
        gravadas = [{"t": a}, {"t": b}, {"t": a}]
        atuais = [{"t": x}, {"t": y}, {"t": y}]
        v = auditoria.comparar(self.registro(gravadas), resposta_ok(atuais))
        assert v.situacao == auditoria.BATE_SEM_TOKENS and v.confere

    def test_contato_compartilhado_que_deixou_de_ser_nao_confere(self):
        # o registro tinha duas linhas com o MESMO contato (o indício de vínculo); a reexecução, dois
        # contatos distintos: só os tokens diferem no texto, mas o padrão de repetição mudou
        a, x, y = "contato:aaaaaaaaaa", "contato:1111111111", "contato:2222222222"
        v = auditoria.comparar(
            self.registro([{"e": "E1", "t": a}, {"e": "E2", "t": a}]),
            resposta_ok([{"e": "E1", "t": x}, {"e": "E2", "t": y}]),
        )
        assert v.situacao == auditoria.DIFERE and not v.confere

    def test_difere_mostra_as_linhas(self):
        v = auditoria.comparar(
            self.registro([{"m": "A", "n": 3}, {"m": "B", "n": 1}]),
            resposta_ok([{"m": "A", "n": 3}, {"m": "B", "n": 2}]),
        )
        assert v.situacao == auditoria.DIFERE and not v.confere
        assert v.soNoRegistro == [{"m": "B", "n": 1}]
        assert v.soNaReexecucao == [{"m": "B", "n": 2}]
        assert v.hashAtual != v.hashRegistrado

    def test_carga_diferente_e_truncado_geram_aviso(self):
        outra = {**CARGAS, "ultimaCarga": "2026-12-01T00:00:00"}
        v = auditoria.comparar(
            self.registro([{"n": 1}], truncado=True), resposta_ok([{"n": 1}], cargas=outra)
        )
        assert v.situacao == auditoria.BATE and v.cargaMudou is True
        assert any("carga do banco mudou" in a for a in v.avisos)
        assert any("TRUNCADO" in a for a in v.avisos)

    def test_reexecucao_com_erro(self):
        v = auditoria.comparar(self.registro([{"n": 1}]), {"erro": "erro_do_banco", "mensagem": "sintaxe"})
        assert v.situacao == auditoria.FALHOU and not v.confere


class TestCli:
    def preparar(self, tmp_path):
        aud = Auditoria(tmp_path / "aud", sessao="20261102T100000-aaaaaa")
        aud.auditar("RETURN 1 AS n", 10, "quantos?", lambda: resposta_ok([{"n": 1}]))
        aud.auditar("RETURN 1 // c", 10, None, lambda: ferramentas.cypher_somente_leitura(None, "x //", 10))
        vazio = tmp_path / "vazio.env"
        vazio.write_text(f"AUDITORIA_DIR={tmp_path / 'aud'}\n", encoding="utf-8")
        return aud, str(vazio)

    def test_sessoes_listar_e_mostrar(self, tmp_path):
        aud, env = self.preparar(tmp_path)
        r = CliRunner().invoke(grupo, ["--env-file", env, "auditoria", "sessoes"])
        assert r.exit_code == 0, r.output
        assert "20261102T100000-aaaaaa" in r.output and "ok=1" in r.output and "recusada=1" in r.output
        r = CliRunner().invoke(grupo, ["--env-file", env, "auditoria", "listar", "--sessao", "ultima"])
        assert r.exit_code == 0, r.output
        assert "quantos?" in r.output and "(sem pergunta)" in r.output
        ident = ler(unico_arquivo(aud))[0]["id"]
        r = CliRunner().invoke(grupo, ["--env-file", env, "auditoria", "mostrar", ident[:10]])
        assert r.exit_code == 0, r.output
        assert json.loads(r.output)["consulta"] == "RETURN 1 AS n"

    def test_verificar_reexecuta_so_os_ok_e_sai_1_se_difere(self, tmp_path, monkeypatch):
        _, env = self.preparar(tmp_path)
        (tmp_path / "vazio.env").write_text(
            f"AUDITORIA_DIR={tmp_path / 'aud'}\nNEO4J_PASSWORD=senha-qualquer\n", encoding="utf-8"
        )
        chamadas = []

        class DriverFalso:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        def executar(driver, consulta, limite):
            chamadas.append((consulta, limite))
            return resposta_ok([{"n": valor}])

        monkeypatch.setattr("sarc.grafo.conexao.abrir_driver", lambda config: DriverFalso())
        monkeypatch.setattr(ferramentas, "cypher_somente_leitura", executar)
        valor = 1
        r = CliRunner().invoke(grupo, ["--env-file", env, "auditoria", "verificar"])
        assert r.exit_code == 0, r.output
        assert "[CONFERE]" in r.output and "bate=1" in r.output
        assert chamadas == [("RETURN 1 AS n", 10)]  # a recusada não é reexecutada
        valor = 2
        r = CliRunner().invoke(grupo, ["--env-file", env, "auditoria", "verificar"])
        assert r.exit_code == 1, r.output
        assert "NÃO CONFERE" in r.output and "só na reexecução" in r.output

    def test_sem_arquivos_e_erro_legivel(self, tmp_path):
        vazio = tmp_path / "v.env"
        vazio.write_text(f"AUDITORIA_DIR={tmp_path / 'nada'}\n", encoding="utf-8")
        r = CliRunner().invoke(grupo, ["--env-file", str(vazio), "auditoria", "listar"])
        assert r.exit_code != 0 and "nenhum arquivo de auditoria" in r.output


# --------------------------------------------------------------------------------------------
# Correções da revisão de 02/11/2026 (D-44)
# --------------------------------------------------------------------------------------------
def erro_neo4j(mensagem, codigo="Neo.ClientError.Statement.SyntaxError"):
    from neo4j.exceptions import ClientError

    return ClientError._hydrate_neo4j(code=codigo, message=mensagem)


DATA_INVALIDA = 'Text cannot be parsed to a Date\n"85999990000"\n ^'


class TestMensagemDoBancoSaneada:
    """Problema 1: a mensagem de erro do Neo4j repete o valor do dado (telefone, CPF, CEP)."""

    def test_literal_e_numero_longo_saem_da_resposta(self):
        r = ferramentas.erro_para_resposta(
            erro_neo4j(DATA_INVALIDA), "MATCH (t:Telefone) RETURN date(t.numero)"
        )
        assert r["erro"] == "erro_do_banco" and r["codigo"] == "Neo.ClientError.Statement.SyntaxError"
        assert "85999990000" not in json.dumps(r)
        assert r["mensagem"].startswith("Text cannot be parsed to a Date")  # o tipo do erro fica

    @pytest.mark.parametrize(
        "dado",
        ["85999990000", "(85) 99999-0000", "123.456.789-09", "12.345.678/0001-90", "60000-000", "ana@x.com"],
    )
    def test_formatos_de_dado_sensivel(self, dado):
        from sarc.mcp_server.privacidade import sanear_mensagem_do_banco

        mensagens = [f'Text cannot be parsed\n"{dado}"', f"got: String('{dado}')"]
        if "@" not in dado:
            mensagens.append(f"got: Long({dado})")  # sem aspas: pega pela sequência de dígitos
        for mensagem in mensagens:
            assert dado not in sanear_mensagem_do_banco(mensagem, "RETURN date(t.x)"), mensagem

    def test_o_que_veio_da_propria_consulta_fica(self):
        # erro de sintaxe: o banco repete o trecho da consulta e o token inválido; a LLM precisa deles
        consulta = "MATCH (n) RETRUN n"
        mensagem = (
            "Invalid input 'RETRUN': expected 'MATCH', ',', 'ORDER BY' or <EOF> "
            "(line 1, column 11 (offset: 10))\n"
            '"MATCH (n) RETRUN n"\n           ^'
        )
        r = ferramentas.erro_para_resposta(erro_neo4j(mensagem), consulta)
        assert "'RETRUN'" in r["mensagem"] and '"MATCH (n) RETRUN n"' in r["mensagem"]
        assert "line 1, column 11 (offset: 10)" in r["mensagem"]
        assert "expected 'MATCH', ',', 'ORDER BY' or <EOF>" in r["mensagem"]  # palavras do Cypher ficam
        assert "<omitido>" not in r["mensagem"]

    @pytest.mark.parametrize(
        "mensagem,dado",
        [  # mensagens reais do Neo4j 5 que repetem o valor SEM aspas
            ("Invalid Regex: Unclosed group near index 11\nJOSE (SILVA", "JOSE (SILVA"),
            ("Invalid Regex: Unclosed character class near index 11\n8599999[0000\n           ^", "8599999"),
            ("Unknown coordinate reference system: JOSE SILVA", "JOSE SILVA"),
            ("Unsupported unit: JOSE SILVA", "JOSE SILVA"),
            (
                "Failed to invoke procedure `db.index.fulltext.queryNodes`: Caused by: "
                "java.lang.IllegalArgumentException: There is no such fulltext schema index: JOSE SILVA",
                "JOSE SILVA",
            ),
        ],
    )
    def test_valor_repetido_sem_aspas(self, mensagem, dado):
        from sarc.mcp_server.privacidade import OMITIDO, sanear_mensagem_do_banco

        consulta = "MATCH (a), (b) CALL db.index.fulltext.queryNodes(a.nome, 'x') RETURN a.nome =~ b.nome"
        saneada = sanear_mensagem_do_banco(mensagem, consulta)
        assert dado not in saneada and OMITIDO in saneada
        assert saneada.split(":")[0] == mensagem.split(":")[0]  # o tipo do erro fica

    def test_regex_invalido_escrito_na_propria_consulta_fica(self):
        from sarc.mcp_server.privacidade import sanear_mensagem_do_banco

        consulta = 'MATCH (n) WHERE n.nome =~ "(" RETURN n'
        mensagem = "Invalid Regex: Unclosed group near index 1\n("
        assert sanear_mensagem_do_banco(mensagem, consulta) == mensagem

    def test_nome_de_funcao_entre_aspas_fica(self):
        from sarc.mcp_server.privacidade import sanear_mensagem_do_banco

        mensagem = (
            "Invalid input for function 'toInteger()': Expected a String, Float, Integer or Boolean, "
            'got: List{String("JOSE SILVA")}'
        )
        saneada = sanear_mensagem_do_banco(mensagem, "MATCH (p) RETURN toInteger([p.nome])")
        assert "'toInteger()'" in saneada and "JOSE SILVA" not in saneada

    def test_sem_consulta_tambem_saneia(self):
        r = ferramentas.erro_para_resposta(erro_neo4j(DATA_INVALIDA))
        assert "85999990000" not in r["mensagem"]

    def test_servidor_saneia_resposta_registro_e_log(self, aud, caplog):
        class DriverQueFalha:
            def session(self, *a, **k):
                raise erro_neo4j(DATA_INVALIDA)

        servidor = criar_servidor(DriverQueFalha(), 5, aud)
        with caplog.at_level(logging.WARNING):
            r = TestServidor().chamar(servidor, {"consulta": "MATCH (t:Telefone) RETURN date(t.numero)"})
        assert r["erro"] == "erro_do_banco" and "85999990000" not in json.dumps(r)
        [reg] = ler(unico_arquivo(aud))
        assert reg["status"] == "erro" and "85999990000" not in json.dumps(reg)
        assert reg["motivo"].startswith("Text cannot be parsed to a Date")
        assert "85999990000" not in caplog.text

    def test_excecao_inesperada_tambem_e_saneada_no_registro(self, aud):
        def explode():
            raise RuntimeError("valor '85999990000' inválido")

        with pytest.raises(RuntimeError):
            aud.auditar("RETURN 1", 10, None, explode)
        [reg] = ler(unico_arquivo(aud))
        assert "85999990000" not in reg["motivo"] and reg["motivo"].startswith("RuntimeError")

    def test_excecao_inesperada_e_saneada_para_o_cliente_e_o_log(self, caplog):
        from sarc.mcp_server.servidor import ErroNaoPrevisto, _protegido

        def explode():
            raise RuntimeError("valor '85999990000' inválido")

        with caplog.at_level(logging.WARNING), pytest.raises(ErroNaoPrevisto) as info:
            _protegido(explode, "RETURN 1")
        assert "85999990000" not in str(info.value) and str(info.value).startswith("RuntimeError")
        assert info.value.__cause__ is None and info.value.__suppress_context__
        assert "85999990000" not in caplog.text

    def test_servidor_nao_manda_excecao_inesperada_crua_ao_cliente(self, aud):
        class DriverQueExplode:
            def session(self, *a, **k):
                raise RuntimeError("valor '85999990000' inválido")

        servidor = criar_servidor(DriverQueExplode(), 5, aud)
        try:
            resultado = asyncio.run(
                servidor.call_tool("cypher_somente_leitura", {"consulta": "MATCH (n) RETURN n"})
            )
            texto = json.dumps([c.model_dump() for c in resultado.content], default=str)
        except Exception as erro:  # noqa: BLE001 - a camada MCP relança (UnexpectedToolError)
            # O traceback inteiro (com causas) é o que um log do lado MCP mostraria.
            texto = "".join(traceback.format_exception(erro))
        assert "85999990000" not in texto
        [reg] = ler(unico_arquivo(aud))
        assert reg["status"] == "erro" and "85999990000" not in json.dumps(reg)


class TestPastaDaAuditoria:
    """Problema 2 (pasta independente do cwd) e problema 5 (permissões)."""

    def test_padrao_relativo_a_raiz_do_projeto_e_nao_ao_cwd(self, monkeypatch, tmp_path):
        from sarc import config

        monkeypatch.delenv("AUDITORIA_DIR", raising=False)
        monkeypatch.chdir("/")
        pasta = config.diretorio_de_auditoria()
        assert pasta.is_absolute()
        assert pasta == config.RAIZ_DO_PROJETO / "logs" / "auditoria"
        assert (config.RAIZ_DO_PROJETO / "pyproject.toml").is_file()
        monkeypatch.setenv("AUDITORIA_DIR", "outra/pasta")
        assert config.diretorio_de_auditoria() == config.RAIZ_DO_PROJETO / "outra" / "pasta"
        monkeypatch.setenv("AUDITORIA_DIR", str(tmp_path / "abs"))
        assert config.diretorio_de_auditoria() == tmp_path / "abs"

    def test_servidor_sem_auditoria_explicita_usa_a_pasta_absoluta(self, monkeypatch, tmp_path):
        monkeypatch.setenv("AUDITORIA_DIR", str(tmp_path / "x"))
        monkeypatch.chdir("/")
        servidor = criar_servidor(None, 5)
        TestServidor().chamar(servidor, {"consulta": "RETURN 1 // c"})
        assert len(auditoria.arquivos_de(tmp_path / "x")) == 1

    def test_pasta_nao_gravavel_e_detectada(self, tmp_path):
        ocupado = tmp_path / "arquivo"
        ocupado.write_text("x")
        assert Auditoria(ocupado).problema_de_gravacao() is not None
        boa = Auditoria(tmp_path / "boa")
        assert boa.problema_de_gravacao() is None
        assert auditoria.arquivos_de(boa.diretorio) == []  # a sonda não deixa arquivo

    def test_pasta_criada_com_0700_e_arquivo_com_0600(self, aud):
        aud.auditar("RETURN 1", 10, None, lambda: resposta_ok([{"a": 1}]))
        assert aud.diretorio.stat().st_mode & 0o777 == 0o700
        assert unico_arquivo(aud).stat().st_mode & 0o777 == 0o600

    def test_arquivo_do_dia_aberto_demais_e_restringido(self, aud, caplog):
        from datetime import datetime

        aud.diretorio.mkdir(mode=0o700)
        arquivo = aud.arquivo(datetime.now().astimezone())
        arquivo.write_text("")
        arquivo.chmod(0o644)
        with caplog.at_level(logging.WARNING, logger="sarc.mcp_server.auditoria"):
            aud.auditar("RETURN 1", 10, None, lambda: resposta_ok([{"a": 1}]))
        assert arquivo.stat().st_mode & 0o777 == 0o600
        assert "0o644" in caplog.text

    def test_inicio_do_servidor_avisa_no_stderr_e_nao_impede(self, tmp_path, monkeypatch):
        ocupado = tmp_path / "arquivo"
        ocupado.write_text("x")
        env = tmp_path / "x.env"
        env.write_text(f"NEO4J_PASSWORD=senha-qualquer\nAUDITORIA_DIR={ocupado}\n", encoding="utf-8")
        subiu = []

        class DriverFalso:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        class ServidorFalso:
            def run(self, transport):
                subiu.append(transport)

        monkeypatch.setattr("sarc.grafo.conexao.abrir_driver", lambda config: DriverFalso())
        monkeypatch.setattr("sarc.mcp_server.servidor.criar_servidor", lambda *a: ServidorFalso())
        r = CliRunner().invoke(grupo, ["--env-file", str(env), "mcp"])
        assert r.exit_code == 0, r.output
        assert subiu == ["stdio"]  # não impede a subida
        assert "ERROR" in r.stderr and "AUDITORIA DESLIGADA" in r.stderr  # o log da CLI vai ao stderr
        assert r.stdout == ""  # stdout é o canal do MCP


class TestIntegridadeDoRegistro:
    """Problema 3: `verificar` e `mostrar` conferem o hash das linhas gravadas."""

    def registro(self, linhas):
        return TestComparacao().registro(linhas) | {"limiteAplicado": 10}

    def test_linhas_editadas_com_hash_mantido(self):
        reg = self.registro([{"n": 1}])
        reg["linhas"] = [{"n": 2}]
        v = auditoria.comparar(reg, resposta_ok([{"n": 2}]))
        assert v.situacao == auditoria.INCONSISTENTE and not v.confere
        assert any("hashLinhas" in a for a in v.avisos)

    def test_ok_sem_linhas_nem_hash(self):
        reg = {"id": "x", "status": "ok", "consulta": "RETURN 1", "nLinhas": 0}
        v = auditoria.comparar(reg, resposta_ok([]))
        assert v.situacao == auditoria.INCONSISTENTE and not v.confere

    def test_nlinhas_diferente(self):
        reg = self.registro([{"n": 1}])
        reg["nLinhas"] = 5
        assert auditoria.problemas_de_integridade(reg)

    def test_registro_integro_nao_tem_problema(self, aud):
        aud.auditar("RETURN 1", 10, None, lambda: resposta_ok([{"x": 0.1 + 0.2, "d": "2022-01-01"}]))
        [reg] = ler(unico_arquivo(aud))
        assert auditoria.problemas_de_integridade(reg) == []

    def test_verificar_nao_reexecuta_o_inconsistente(self):
        chamado = []
        reg = self.registro([{"n": 1}])
        reg["linhas"] = [{"n": 2}]

        class Driver:
            def session(self, *a, **k):
                chamado.append(1)
                raise AssertionError

        assert auditoria.verificar(Driver(), reg).situacao == auditoria.INCONSISTENTE
        assert chamado == []

    def _env(self, tmp_path):
        env = tmp_path / "x.env"
        env.write_text(f"AUDITORIA_DIR={tmp_path / 'aud'}\nNEO4J_PASSWORD=senha-qualquer\n", encoding="utf-8")
        return str(env)

    def _adulterar(self, tmp_path):
        aud = Auditoria(tmp_path / "aud", sessao="S")
        aud.auditar("RETURN 1 AS n", 10, None, lambda: resposta_ok([{"n": 1}]))
        arquivo = unico_arquivo(aud)
        reg = ler(arquivo)[0]
        reg["linhas"] = [{"n": 2}]
        arquivo.write_text(json.dumps(reg) + "\n", encoding="utf-8")
        return reg

    def test_cli_verificar_conta_como_falha(self, tmp_path, monkeypatch):
        self._adulterar(tmp_path)

        class DriverFalso:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        monkeypatch.setattr("sarc.grafo.conexao.abrir_driver", lambda config: DriverFalso())
        monkeypatch.setattr(ferramentas, "cypher_somente_leitura", lambda *a: resposta_ok([{"n": 2}]))
        r = CliRunner().invoke(grupo, ["--env-file", self._env(tmp_path), "auditoria", "verificar"])
        assert r.exit_code == 1, r.output
        assert "NÃO CONFERE" in r.output and "registro_inconsistente=1" in r.output

    def test_cli_mostrar_avisa(self, tmp_path):
        reg = self._adulterar(tmp_path)
        r = CliRunner().invoke(grupo, ["--env-file", self._env(tmp_path), "auditoria", "mostrar", reg["id"]])
        assert r.exit_code == 1
        assert json.loads(r.stdout)["linhas"] == [{"n": 2}]
        assert "INCONSISTENTE" in r.stderr


class TestToleranciaNumerica:
    """Problema 4: soma de ponto flutuante que varia nos últimos dígitos."""

    def registro(self, linhas):
        return TestComparacao().registro(linhas)

    def test_float_nos_ultimos_digitos_bate_com_tolerancia(self):
        v = auditoria.comparar(
            self.registro([{"m": "A", "soma": 0.30000000000000004}, {"m": "B", "soma": 1e6}]),
            resposta_ok([{"m": "A", "soma": 0.3}, {"m": "B", "soma": 1e6 + 1e-7}]),
        )
        assert v.situacao == auditoria.BATE_COM_TOLERANCIA and v.confere
        assert any("tolerância" in a for a in v.avisos)

    def test_tolerancia_vale_fora_de_ordem(self):
        v = auditoria.comparar(
            self.registro([{"m": "A", "s": 0.1 + 0.2}, {"m": "B", "s": 2.5}]),
            resposta_ok([{"m": "B", "s": 2.5}, {"m": "A", "s": 0.3}]),
        )
        assert v.situacao == auditoria.BATE_COM_TOLERANCIA

    def test_centavos_numa_soma_de_bilhoes_difere(self):
        # com tolerância relativa de 1e-9, R$ 1,40 de diferença em R$ 1,5 bilhão passava como "confere"
        v = auditoria.comparar(self.registro([{"total": 1.5e9}]), resposta_ok([{"total": 1.5e9 + 1.4}]))
        assert v.situacao == auditoria.DIFERE and not v.confere
        v = auditoria.comparar(self.registro([{"total": 1.5e9}]), resposta_ok([{"total": 1.5e9 + 0.01}]))
        assert v.situacao == auditoria.DIFERE

    def test_diferenca_real_em_float_difere(self):
        v = auditoria.comparar(self.registro([{"s": 0.3}]), resposta_ok([{"s": 0.3001}]))
        assert v.situacao == auditoria.DIFERE

    @pytest.mark.parametrize(
        ("antes", "depois"),
        [
            (10**17, 10**17 + 1),  # inteiro: relativo minúsculo, mas não se afrouxa
            ("0.30000000000000004", "0.3"),  # texto
            ("2022-01-01", "2022-01-02"),  # data
            (1, 1.0),  # inteiro x float não é a mesma coisa
            (True, 1.0),
        ],
    )
    def test_inteiro_texto_e_data_nao_se_afrouxam(self, antes, depois):
        v = auditoria.comparar(self.registro([{"v": antes}]), resposta_ok([{"v": depois}]))
        assert v.situacao == auditoria.DIFERE, (antes, depois)


class TestSessaoUltima:
    """Problema 6: `--sessao ultima` pelo momento mais recente, ignorando registro sem sessão."""

    def test_pelo_momento_e_nao_pelo_texto(self):
        from sarc.mcp_server.auditoria_cli import _filtrar

        registros = [
            {"id": "1", "sessao": "zzz-antiga", "momento": "2026-11-01T10:00:00.000-03:00"},
            {"id": "2", "sessao": "aaa-nova", "momento": "2026-11-02T10:00:00.000-03:00"},
            {"id": "3", "sessao": None, "momento": "2026-11-03T10:00:00.000-03:00"},
            {"id": "4", "momento": "2026-11-04T10:00:00.000-03:00"},
        ]
        assert [r["id"] for r in _filtrar(registros, "ultima", None)] == ["2"]

    def test_momento_com_fusos_diferentes(self):
        from sarc.mcp_server.auditoria_cli import _filtrar

        registros = [
            # como texto, o primeiro é o maior; no tempo, o segundo é o mais recente (13:00 UTC)
            {"id": "1", "sessao": "B", "momento": "2026-11-02T12:00:00.000+00:00"},
            {"id": "2", "sessao": "A", "momento": "2026-11-02T10:00:00.000-03:00"},
        ]
        assert [r["id"] for r in _filtrar(registros, "ultima", None)] == ["2"]

    def test_sem_nenhuma_sessao_e_erro_legivel(self):
        import click

        from sarc.mcp_server.auditoria_cli import _filtrar

        with pytest.raises(click.ClickException):
            _filtrar([{"id": "1", "sessao": None, "momento": "2026-11-02T10:00:00+00:00"}], "ultima", None)


class TestTrilhaDeChamadas:
    """D-45: toda tool chamada deixa uma linha curta em `chamadas-AAAA-MM-DD.jsonl`, com a pergunta."""

    def trilha(self, aud):
        arquivos = auditoria.arquivos_de(aud.diretorio, auditoria.PREFIXO_CHAMADAS)
        assert len(arquivos) == 1 and arquivos[0].name.startswith("chamadas-")
        return ler(arquivos[0])

    def chamar(self, servidor, tool, argumentos):
        try:
            resultado = asyncio.run(servidor.call_tool(tool, argumentos))
        except Exception:  # noqa: BLE001 - a camada MCP relança o erro não previsto
            return None
        return json.loads(resultado.content[0].text)

    def test_registra_ok_erro_e_excecao_sem_guardar_o_resultado(self, aud):
        aud.registrar_chamada("resumo_da_carga", {}, "o que tem?", lambda: {"dados": {"segredo": 1}})
        aud.registrar_chamada(
            "vinculos_da_empresa", {"cnpj": "x"}, None, lambda: {"erro": "parametro_invalido"}
        )

        def explode():
            raise RuntimeError("falhou")

        with pytest.raises(RuntimeError):
            aud.registrar_chamada("esquema_do_grafo", {}, None, explode)
        ok, erro, excecao = self.trilha(aud)
        assert ok["tool"] == "resumo_da_carga" and ok["status"] == "ok" and ok["erro"] is None
        assert ok["pergunta"] == "o que tem?" and ok["sessao"] == "S1" and ok["momento"]
        assert "segredo" not in json.dumps(ok)
        assert erro["status"] == "erro" and erro["erro"] == "parametro_invalido"
        assert erro["parametros"] == {"cnpj": "x"} and erro["pergunta"] is None
        assert excecao["status"] == "erro" and excecao["erro"] == "RuntimeError"
        assert not auditoria.arquivos_de(aud.diretorio)  # nada no arquivo do Cypher livre

    def test_toda_tool_aceita_pergunta_e_as_instrucoes_pedem(self, aud):
        from sarc.mcp_server.servidor import INSTRUCOES

        servidor = criar_servidor(None, 5, aud)
        for tool in asyncio.run(servidor.list_tools()):
            assert "pergunta" in tool.input_schema["properties"], tool.name
            assert "pergunta" in tool.description, tool.name
            obrigatorios = tool.input_schema.get("required") or []
            assert ("pergunta" in obrigatorios) == (tool.name == "recusar_pergunta"), tool.name
        assert "Em TODA chamada, de qualquer tool" in INSTRUCOES

    def test_servidor_registra_validada_recusa_e_cypher_ligado_ao_registro(self, aud):
        servidor = criar_servidor(None, 5, aud)  # driver None: a tool validada falha sem banco
        assert self.chamar(servidor, "resumo_da_carga", {"pergunta": "o que está carregado?"}) is None
        recusa = self.chamar(
            servidor, "recusar_pergunta", {"tipo": "afirmacao_nao_sustentada", "pergunta": "fraudou?"}
        )
        assert recusa["recusa"] == "afirmacao_nao_sustentada"
        livre = self.chamar(
            servidor, "cypher_somente_leitura", {"consulta": "RETURN 1 // c", "pergunta": "um?"}
        )
        assert livre["erro"] == "consulta_nao_permitida"
        resumo, rec, cypher = self.trilha(aud)
        assert (resumo["tool"], resumo["status"], resumo["pergunta"]) == (
            "resumo_da_carga",
            "erro",
            "o que está carregado?",
        )
        assert (rec["tool"], rec["status"], rec["pergunta"]) == ("recusar_pergunta", "ok", "fraudou?")
        assert rec["parametros"] == {"tipo": "afirmacao_nao_sustentada"}
        assert (cypher["tool"], cypher["status"], cypher["pergunta"]) == (
            "cypher_somente_leitura",
            "recusada",
            "um?",
        )
        [registro] = ler(unico_arquivo(aud))
        assert cypher["registroCypher"] == registro["id"] and registro["pergunta"] == "um?"
        assert resumo["registroCypher"] is None
        assert len({resumo["sessao"], rec["sessao"], cypher["sessao"]}) == 1

    def test_cli_chamadas_mostra_a_trilha_com_pergunta_e_consulta(self, tmp_path):
        aud = Auditoria(tmp_path / "aud", sessao="20261107T100000-bbbbbb")
        servidor = criar_servidor(None, 5, aud)
        self.chamar(
            servidor, "recusar_pergunta", {"tipo": "dado_nao_responde", "pergunta": "qual o parentesco?"}
        )
        self.chamar(servidor, "cypher_somente_leitura", {"consulta": "RETURN 1 // c", "pergunta": "um?"})
        env = tmp_path / "vazio.env"
        env.write_text(f"AUDITORIA_DIR={tmp_path / 'aud'}\n", encoding="utf-8")
        r = CliRunner().invoke(grupo, ["--env-file", str(env), "auditoria", "chamadas", "--sessao", "ultima"])
        assert r.exit_code == 0, r.output
        assert r.output.index("recusar_pergunta") < r.output.index("cypher_somente_leitura")
        assert "P: qual o parentesco?" in r.output and "tipo=dado_nao_responde" in r.output
        assert "P: um?" in r.output and "C: [" in r.output and "RETURN 1 // c" in r.output
