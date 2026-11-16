"""Contrato das tools do MCP, sem banco: parâmetros, tetos, máscara, recusas e envelope."""

from __future__ import annotations

from datetime import date

import pytest
from neo4j.exceptions import ClientError

from sarc.conluio.consultas import (
    AUXILIAR,
    DESCONHECIDA,
    ENTRADA_ANTERIOR,
    ENTRADA_POSTERIOR,
    PRINCIPAL,
    Ocorrencia,
    ResultadoConluio,
    montar_ocorrencia,
    vinculo_societario,
)
from sarc.mcp_server import ferramentas, privacidade, recusas
from sarc.mcp_server.resposta import Resposta, para_json


class TestParametros:
    @pytest.mark.parametrize(("pedido", "aplicado"), [(None, 20), (0, 1), (-5, 1), (50, 50), (10_000, 200)])
    def test_limite_tem_teto(self, pedido, aplicado):
        assert ferramentas.limitar(pedido) == aplicado

    def test_datas(self):
        assert ferramentas.data_de("2020") == date(2020, 1, 1)
        assert ferramentas.data_de("2020", fim=True) == date(2020, 12, 31)
        assert ferramentas.data_de("2021-08-10") == date(2021, 8, 10)
        assert ferramentas.data_de(None) is None
        with pytest.raises(ferramentas.ErroDeEntrada):
            ferramentas.data_de("10/08/2021")

    def test_cnpj(self):
        assert ferramentas.cnpj_de("11.111.111/0001-01") == "11111111000101"
        with pytest.raises(ferramentas.ErroDeEntrada):
            ferramentas.cnpj_de("11111111")


class TestPrivacidade:
    def test_token_estavel_e_sem_o_valor(self):
        t = privacidade.token("85999990000")
        assert t == privacidade.token("85999990000")
        assert t != privacidade.token("85999990001")
        assert "85999990000" not in t

    def test_ocorrencia_mascara_contato_e_pessoa(self):
        ocorrencia = {
            "vinculos": [
                {"contato": "4:abc:20", "caminho": None},
                {
                    "contato": None,
                    "caminho": [{"cnpjBasico": "11111111"}, {"aresta": "SOCIO"}, {"pessoa": "p1"}],
                },
            ]
        }
        saida = privacidade.mascarar_ocorrencia(ocorrencia)
        assert saida["vinculos"][0]["contato"].startswith("contato:")
        assert saida["vinculos"][1]["caminho"][2]["pessoa"].startswith("pessoa:")
        assert saida["vinculos"][1]["caminho"][0] == {"cnpjBasico": "11111111"}


class TestRecusas:
    @pytest.mark.parametrize("tipo", recusas.TIPOS)
    def test_tres_classes_distintas_e_nao_vazias(self, tipo):
        r = recusas.recusar(tipo, "pergunta")
        assert r["recusa"] == tipo
        assert r["respostaVazia"] is False
        assert r["porque"] and r["oQueFazer"]

    def test_textos_nao_colapsam(self):
        assert len({recusas.recusar(t, "p")["porque"] for t in recusas.TIPOS}) == 3

    def test_tipo_desconhecido(self):
        assert recusas.recusar("outro", "p")["recusa"] == "tipo_de_recusa_desconhecido"


class TestEnvelope:
    def test_resposta_carrega_consulta_limite_garantias_e_regime(self):
        d = Resposta(
            {"x": date(2020, 1, 1)}, "MATCH (n) RETURN n", "indício", {"cargas": []}
        ).para_dicionario()
        assert set(d) == {"dados", "consultaExecutada", "limiteDeLeitura", "garantiasDeMetodo", "regime"}
        assert d["dados"]["x"] == "2020-01-01"

    def test_para_json_converte_tipos_do_driver(self):
        from neo4j.time import Date

        assert para_json({"a": [Date(2020, 1, 2)], "b": (1, 2)}) == {"a": ["2020-01-02"], "b": [1, 2]}


def _ocorrencia(classe_vinculo: tuple[str, str], cnpj_b: str = "2") -> Ocorrencia:
    nivel, confianca = classe_vinculo
    linha = {
        "licitacao": "087|X",
        "municipio": "087",
        "dataCertame": date(2020, 1, 1),
        "cnpjA": "1",
        "nomeA": None,
        "cnpjB": cnpj_b,
        "nomeB": None,
        "caminhoValido": nivel == PRINCIPAL,
        "nos": [{"cnpjBasico": "1"}, {"pessoa": "p"}, {"cnpjBasico": "2"}],
        "arestas": [
            {"tipo": "SOCIO", "dataEntrada": date(2010, 1, 1)},
            {"tipo": "SOCIO", "dataEntrada": None if confianca == DESCONHECIDA else date(2021, 1, 1)},
        ],
        "contatos505": [],
        "contatosRfb": [],
    }
    ocorrencia, _ = montar_ocorrencia(linha, 5)
    assert ocorrencia is not None
    return ocorrencia


class TestSemTotalSomado:
    """F-05/F-12: as três listas saem separadas e não existe campo de total."""

    def test_dados_de_conluio_separados_e_sem_total(self):
        resultado = ResultadoConluio(
            ocorrencias=[
                _ocorrencia((PRINCIPAL, ENTRADA_ANTERIOR)),
                _ocorrencia((AUXILIAR, DESCONHECIDA), "3"),
                _ocorrencia((AUXILIAR, ENTRADA_POSTERIOR), "4"),
            ],
            licitacoesPorSituacao={"avaliada": 1},
            paresDeParticipantes=6,
            paresMesmaRaiz=0,
            contatosSemNumeroIgnorados=0,
        )
        dados = ferramentas._dados_conluio(resultado, 20)
        assert [
            len(dados[k]) for k in ("alertas", "alertasConfiancaDesconhecida", "vinculosPosterioresAoCertame")
        ] == [1, 1, 1]
        chaves = set(dados) | set(dados["contagens"])
        assert not any("total" in k.lower() for k in chaves), chaves

    def test_lista_respeita_o_limite_mas_a_contagem_nao(self):
        resultado = ResultadoConluio(
            ocorrencias=[_ocorrencia((PRINCIPAL, ENTRADA_ANTERIOR), str(i)) for i in range(5)],
            licitacoesPorSituacao={},
            paresDeParticipantes=0,
            paresMesmaRaiz=0,
            contatosSemNumeroIgnorados=0,
        )
        dados = ferramentas._dados_conluio(resultado, 2)
        assert len(dados["alertas"]) == 2
        assert dados["contagens"]["ocorrenciasPrincipal"] == 5


class TestClassificacaoDoCaminho:
    def test_tipos(self):
        d = date(2010, 1, 1)
        um = [{"tipo": "SOCIO_PJ", "dataEntrada": d}]
        assert vinculo_societario([{}, {}], um, True).tipoVinculo == "holding_direta"
        dois = [{"tipo": "SOCIO_PJ", "dataEntrada": d}] * 2
        assert vinculo_societario([{}, {"cnpjBasico": "x"}, {}], dois, True).tipoVinculo == "socio_pj"
        rep = [{"tipo": "REPRESENTA"}, {"tipo": "SOCIO", "dataEntrada": d}]
        v = vinculo_societario([{}, {"pessoa": "p"}, {}], rep, False)
        assert (v.tipoVinculo, v.nivel, v.confiancaTemporal, v.dataVinculo) == (
            "representante",
            AUXILIAR,
            DESCONHECIDA,
            None,
        )
        quatro = [{"tipo": "SOCIO", "dataEntrada": d}] * 4
        assert vinculo_societario([{}] * 5, quatro, True).tipoVinculo == "cadeia_societaria"


class TestErros:
    def test_escrita_recusada_vira_mensagem(self):
        erro = ClientError._hydrate_neo4j(code="Neo.ClientError.Statement.AccessMode", message="x")
        assert ferramentas.erro_para_resposta(erro)["erro"] == "escrita_recusada_pelo_banco"

    def test_timeout_vira_mensagem(self):
        erro = ClientError._hydrate_neo4j(
            code="Neo.ClientError.Transaction.TransactionTimedOutClientConfiguration", message="x"
        )
        assert ferramentas.erro_para_resposta(erro)["erro"] == "tempo_esgotado"

    def test_parametro_invalido(self):
        assert ferramentas.erro_para_resposta(ferramentas.ErroDeEntrada("x"))["erro"] == "parametro_invalido"


class TestTresIndicios:
    def test_instrucoes_cobrem_os_tres_indicios_e_proibem_somar(self):
        from sarc.mcp_server.servidor import INSTRUCOES

        for termo in ("CONLUIO", "ADITIVAÇÃO", "PRETERIÇÃO", "NUNCA se somam", "resumo_da_carga"):
            assert termo in INSTRUCOES
        assert "não calculada" in INSTRUCOES

    @pytest.mark.parametrize("termo", ["aditivo", "ordem cronológica"])
    def test_recusa_de_culpa_vale_para_os_indicios_novos(self, termo):
        assert termo in recusas.recusar(recusas.AFIRMACAO_NAO_SUSTENTADA, "p")["porque"]

    def test_limite_de_leitura_da_aditivacao_e_indicio(self):
        assert ferramentas.LIMITE_DE_LEITURA_ADITIVACAO.startswith("Indício, não acusação")
        assert "NUNCA se somam" in ferramentas.LIMITE_DE_LEITURA_ADITIVACAO
