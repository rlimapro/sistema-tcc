"""Transformador e domínio. Os registros partem da fixture real (`tests/fixtures/tce/cache`)."""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

import pytest

from sarc.etl_tce import transformador as tr
from sarc.etl_tce.dominio import eh_licitacao_de_fato

FIXTURE = Path(__file__).parent.parent / "fixtures" / "tce" / "cache"

ORGAO_ADESAO = (
    "SOP - SUPERINTENDENCIA DE OBRAS PUBLICAS, Municipio de Fortaleza, Ceara, "
    "registro de precos original 20180010DAE"
)
ORGAO_SRP_PROPRIO = "Secretaria de Educacao/FME, Municipio de Quixada, Ceara, Exercicio de 2020"


def _ler(endpoint: str, municipio: str, ano: int | str) -> list[dict]:
    return json.loads((FIXTURE / endpoint / municipio / f"{ano}.json").read_text(encoding="utf-8"))


def _processos() -> list[dict]:
    return _ler("processos_administrativos_contratacoes", "087", 2021) + _ler(
        "processos_administrativos_contratacoes", "170", 2009
    )


class TestLicitacaoDeFato:
    def test_especie_n(self):
        assert eh_licitacao_de_fato("N", None)

    def test_srp_proprio_rotulado_r_entra(self):
        assert eh_licitacao_de_fato(" R ", ORGAO_SRP_PROPRIO)

    def test_adesao_fica_fora_mesmo_com_as_duas_marcas(self):
        assert not eh_licitacao_de_fato("R", ORGAO_ADESAO)
        assert not eh_licitacao_de_fato("R", f"{ORGAO_ADESAO}, Exercicio de 2020")

    @pytest.mark.parametrize("orgao", ["", None, "secretaria de educacao"])
    def test_r_sem_marca_fica_fora(self, orgao):
        assert not eh_licitacao_de_fato("R", orgao)

    @pytest.mark.parametrize("especie", ["D", "F", "I", "C", "Q", "P", "M", "Z"])
    def test_outras_especies_ficam_fora(self, especie):
        assert not eh_licitacao_de_fato(especie, ORGAO_SRP_PROPRIO, "6")

    @pytest.mark.parametrize("modalidade", ["1", "2", "3", "6", " 6 "])
    def test_especie_vazia_com_modalidade_de_disputa_entra(self, modalidade):
        """2009: a espécie não era preenchida; a modalidade (campo 18) decide. Trairi 2009.01.20.2 é '6'."""
        assert eh_licitacao_de_fato(None, None, modalidade)
        assert eh_licitacao_de_fato("", None, modalidade)

    @pytest.mark.parametrize("modalidade", ["9", "", None, "X"])
    def test_especie_vazia_sem_modalidade_de_disputa_fica_fora(self, modalidade):
        assert not eh_licitacao_de_fato(None, None, modalidade)


class TestProcessos:
    def test_chave_colidida_marcada_e_contada(self):
        """'005/2021-PC' e '005/2021-PC.' normalizam para a mesma chave e são processos distintos."""
        contagens: Counter[str] = Counter()
        linhas = {x["id"]: x for x in tr.transformar_processos(_processos(), contagens)}
        assert linhas["087|0052021PC"]["chaveColidida"] is True
        assert linhas["087|0052021PC"]["nRegistrosColididos"] == 2
        assert linhas["087|2901012021CP"]["chaveColidida"] is False
        assert contagens["licitacao_chave_colidida"] == 1

    def test_datas_autuacao_e_sessao(self):
        """dataRealizacao é a autuação (D-05a); a sessão vai à parte."""
        linhas = {x["id"]: x for x in tr.transformar_processos(_processos(), Counter())}
        assert linhas["087|2901012021CP"]["dataRealizacao"] == "2021-01-27"
        assert linhas["087|2901012021CP"]["dataSessao"] == "2021-03-05"

    def test_sessao_implausivel_descartada_e_contada(self):
        bruto = dict(_ler("processos_administrativos_contratacoes", "087", 2021)[0])
        contagens: Counter[str] = Counter()
        for sessao in ("2020-12-01T00:00:00", "2023-01-01T00:00:00"):
            bruto["data_realizacao_licitacao"] = sessao
            assert tr.transformar_processos([bruto], contagens)[0]["dataSessao"] is None
        assert contagens["data_sessao_descartada"] == 2

    def test_srp_proprio_e_trairi_sao_de_fato(self):
        contagens: Counter[str] = Counter()
        linhas = {x["id"]: x for x in tr.transformar_processos(_processos(), contagens)}
        assert linhas["087|2901012021CP"]["licitacaoDeFato"] is True
        assert linhas["087|0052021PC"]["licitacaoDeFato"] is False  # adesão a ata de Choró
        assert linhas["170|200901202"]["licitacaoDeFato"] is True
        assert linhas["170|200901202"]["especie"] is None
        assert contagens["licitacao_de_fato_por_modalidade"] == 1

    def test_colisao_de_fato_vence_qualquer_data(self):
        base = _ler("processos_administrativos_contratacoes", "087", 2021)[0]
        de_fato = dict(
            base, modalidade_processo_administrativo="N", data_realizacao_autuacao_licitacao="2021-01-01"
        )
        dispensa = dict(
            base, modalidade_processo_administrativo="D", data_realizacao_autuacao_licitacao="2021-12-01"
        )
        for ordem in ([de_fato, dispensa], [dispensa, de_fato]):
            linha = tr.transformar_processos(ordem, Counter())[0]
            assert linha["especie"] == "N" and linha["dataRealizacao"] == "2021-01-01"


class TestLicitantes:
    def _transformar(self, brutos, de_fato=None):
        contagens: Counter[str] = Counter()
        if de_fato is None:
            de_fato = {
                x["id"] for x in tr.transformar_processos(_processos(), Counter()) if x["licitacaoDeFato"]
            }
        return (*tr.transformar_licitantes(brutos, de_fato, contagens), contagens)

    def test_pf_descartada_e_nunca_gravada(self):
        brutos = _ler("licitantes_fornecedores_bens_servicos", "087", 2021)
        participacoes, telefones, enderecos, contagens = self._transformar(brutos)
        assert contagens["licitante_pf_descartado"] == 1
        texto = json.dumps([participacoes, telefones, enderecos])
        assert "ANONIMIZADA" not in texto and "AAAAAAAA" not in texto

    def test_cnpj_invalido_descartado_e_contado(self):
        bruto = _ler("licitantes_fornecedores_bens_servicos", "087", 2021)[0]
        brutos = [dict(bruto, numero_documento_negociante=d) for d in ("", None, "123", "  ")]
        participacoes, _, _, contagens = self._transformar(brutos)
        assert participacoes == []
        assert contagens["licitante_cnpj_invalido"] == 4

    def test_sem_data_descartado_e_contado(self):
        """MERGE de PARTICIPOU_DE com data nula é erro no Neo4j: a linha sai antes, contada."""
        bruto = _ler("licitantes_fornecedores_bens_servicos", "087", 2021)[0]
        participacoes, _, _, contagens = self._transformar(
            [dict(bruto, data_realizacao_licitacao=d) for d in (None, " ")]
        )
        assert participacoes == [] and contagens["licitante_sem_data"] == 2

    def test_dv_invalido_contado_e_mantido(self):
        bruto = _ler("licitantes_fornecedores_bens_servicos", "087", 2021)[0]
        participacoes, _, _, contagens = self._transformar(
            [dict(bruto, numero_documento_negociante="08458279000164")]
        )
        assert len(participacoes) == 1 and contagens["licitante_cnpj_dv_invalido"] == 1

    def test_tipo_desconhecido_contado(self):
        bruto = _ler("licitantes_fornecedores_bens_servicos", "087", 2021)[0]
        participacoes, _, _, contagens = self._transformar([dict(bruto, codigo_tipo_negociante="3")])
        assert participacoes == [] and contagens["licitante_tipo_desconhecido"] == 1

    def test_participacao_leva_a_data_do_certame(self):
        brutos = _ler("licitantes_fornecedores_bens_servicos", "087", 2021)
        participacoes, _, _, _ = self._transformar(brutos)
        par = [p for p in participacoes if p["idLicitacao"] == "087|2901012021CP"]
        assert {p["dataRealizacao"] for p in par} == {"2021-01-27"}
        assert all(len(p["cnpj"]) == 14 and p["cnpjBasico"] == p["cnpj"][:8] for p in participacoes)

    def test_duplicata_de_participacao_vira_uma_linha(self):
        bruto = _ler("licitantes_fornecedores_bens_servicos", "087", 2021)[0]
        participacoes, _, _, _ = self._transformar([bruto, dict(bruto)])
        assert len(participacoes) == 1

    def test_contato_bruto_vai_para_a_participacao(self):
        """Entrada do vínculo aproximado (D-47): o contato como foi digitado, sem repetir."""
        base = _ler("licitantes_fornecedores_bens_servicos", "087", 2021)[0]
        bruto = dict(base, fone_negociante=" 08130933317", endereco_negociante="R Y, 2", cep_negociante="2")
        outro = dict(bruto, fone_negociante="(81)3093331", endereco_negociante="R X, 1", cep_negociante="1")
        vazio = dict(bruto, fone_negociante="  ", endereco_negociante=None)
        participacoes, _, _, _ = self._transformar([bruto, dict(bruto), outro, vazio])
        (p,) = participacoes
        assert p["foneDeclarado"] == ["08130933317", "(81)3093331"]
        assert p["enderecoDeclarado"] == ["R Y, 2", "R X, 1"]
        assert p["cepDeclarado"] == ["2", "1"]

    def test_par_do_acordao_declara_o_mesmo_endereco(self):
        brutos = _ler("licitantes_fornecedores_bens_servicos", "087", 2021)
        _, _, enderecos, _ = self._transformar(brutos)
        no_certame = {e["cnpj"]: e["idEndereco"] for e in enderecos if e["idLicitacao"] == "087|1308022021PE"}
        assert no_certame["27499707000140"] == no_certame["32490833000174"] == "61760000|411|ZEZITO"

    def test_trairi_mesmo_telefone_declarado(self):
        brutos = _ler("licitantes_fornecedores_bens_servicos", "170", 2009)
        _, telefones, enderecos, _ = self._transformar(brutos)
        assert {t["telefone"] for t in telefones} == {"8533511606"}
        assert len(telefones) == 3
        assert {t["data"] for t in telefones} == {"2009-02-04"}
        # Os irmãos da loja 05 têm o mesmo CEP; o da loja 03 declara outro CEP (60165010).
        assert sorted(e["idEndereco"] for e in enderecos) == [
            "60165010|1001",
            "60165011|1001",
            "60165011|1001",
        ]

    def test_contato_so_em_licitacao_de_fato(self):
        brutos = _ler("licitantes_fornecedores_bens_servicos", "087", 2021)
        participacoes, telefones, enderecos, contagens = self._transformar(brutos, de_fato=set())
        assert participacoes and telefones == [] and enderecos == []
        # Conta registros PJ (com duplicatas da fonte), não participações deduplicadas.
        assert (
            contagens["contato_fora_de_licitacao_de_fato"]
            == len(brutos) - contagens["licitante_pf_descartado"]
        )

    def test_sem_numero_ou_sem_telefone_contado(self):
        brutos = _ler("licitantes_fornecedores_bens_servicos", "087", 2021)
        _, _, _, contagens = self._transformar(brutos)
        assert contagens["contato_endereco_sem_chave"] >= 3  # S/N e sem número no 1308.02/2021-PE
        assert contagens["contato_telefone_sem_chave"] >= 1


class TestContratos:
    def _contratos_2024(self, contagens=None):
        return tr.transformar_contratos(
            _ler("contratos", "087", 2024), contagens if contagens is not None else Counter()
        )

    def test_original_sem_sufixo_e_aditivo_com_sufixo(self):
        linhas = self._contratos_2024()
        originais = [c for c in linhas if c["classe"] == "original"]
        aditivos = [c for c in linhas if c["classe"] != "original"]
        assert originais and aditivos
        assert all("#" not in c["id"] for c in originais)
        assert all("#" in c["id"] and c["chaveNegociante"] == c["id"].split("#")[0] for c in aditivos)

    def test_originado_de_so_para_original_com_processo(self):
        vinculos = tr.vinculos_originado_de(self._contratos_2024())
        assert vinculos and {v["idLicitacao"] for v in vinculos} == {"087|2603012024PE"}
        assert all("#" not in v["idContrato"] for v in vinculos)

    def test_valor_divergente_marcado_e_espurio(self):
        bruto = _ler("contratos", "087", 2024)[0]
        contagens: Counter[str] = Counter()
        linhas = tr.transformar_contratos(
            [dict(bruto, valor_total_contrato=10.0), dict(bruto, valor_total_contrato=20.0)], contagens
        )
        assert len(linhas) == 1 and linhas[0]["valorTotal"] is None and linhas[0]["valorDivergente"] is True
        espurio = dict(bruto, numero_contrato="2017062208", valor_total_contrato=2017062208.0)
        assert tr.transformar_contratos([espurio], contagens)[0]["valorEspurio"] is True
        assert not tr.valor_igual_numero_contrato(800.0, "800")
        assert contagens["contrato_valor_divergente"] == 1 and contagens["contrato_valor_espurio"] == 1

    def test_aditivo_aponta_o_id_do_original(self):
        linhas = {c["id"]: c for c in self._contratos_2024()}
        aditivos = [c for c in linhas.values() if c["classe"] != "original"]
        assert aditivos and all(linhas[a["idOriginal"]]["classe"] == "original" for a in aditivos)
        assert all(c["idOriginal"] is None for c in linhas.values() if c["classe"] == "original")

    def test_iguatu_original_que_aponta_para_si_mesmo_nao_tem_id_original(self):
        # Iguatu preenche numero_contrato_original no próprio OR (com o próprio número e data vazia).
        contagens: Counter[str] = Counter()
        linhas = tr.transformar_contratos(_ler("contratos", "074", 2019), contagens)
        original = next(c for c in linhas if c["classe"] == "original")
        assert original["id"] == "074|2019.01.18.07|2019-01-18" and original["idOriginal"] is None
        aditivos = [c for c in linhas if c["classe"] == "acrescimo"]
        assert len(aditivos) == 3 and {a["idOriginal"] for a in aditivos} == {original["id"]}
        assert contagens["aditivo_sem_numero_original"] == 0

    def test_vigencia_e_tipo_do_objeto(self):
        linha = next(
            c
            for c in self._contratos_2024()
            if c["id"] == "087|20240412000201|2024-12-30#202404120002|2024-04-12"
        )
        assert linha["dataFimVigencia"] == "2025-04-12" and linha["tipoObjeto"] == "O"

    def test_aditivo_sem_numero_do_original_contado(self):
        bruto = next(r for r in _ler("contratos", "087", 2024) if r["modalide_contrato"] == "AP")
        contagens: Counter[str] = Counter()
        linha = tr.transformar_contratos(
            [dict(bruto, numero_contrato_original="  ", data_contrato_original=None)], contagens
        )[0]
        assert linha["idOriginal"] is None and "#" not in linha["id"]
        assert contagens["aditivo_sem_numero_original"] == 1

    def test_modalidade_desconhecida_contada(self):
        bruto = _ler("contratos", "087", 2024)[0]
        contagens: Counter[str] = Counter()
        linha = tr.transformar_contratos([dict(bruto, modalide_contrato="ZZ")], contagens)[0]
        assert linha["classe"] == "desconhecida" and contagens["contrato_modalidade_desconhecida"] == 1


class TestContratados:
    def test_pf_e_cnpj_invalido_descartados(self):
        brutos = _ler("contratados", "087", 2021)
        contagens: Counter[str] = Counter()
        linhas = tr.transformar_contratados(
            brutos + [dict(brutos[0], numero_documento_negociante="1")], contagens
        )
        assert contagens["contratado_pf_descartado"] == 1
        assert contagens["contratado_cnpj_invalido"] == 1
        assert all(len(x["cnpj"]) == 14 for x in linhas)
        assert "ANONIMIZADA" not in json.dumps(linhas)

    def test_chave_negociante_casa_com_o_contrato(self):
        contratos = {
            c["chaveNegociante"] for c in tr.transformar_contratos(_ler("contratos", "087", 2024), Counter())
        }
        contratados = tr.transformar_contratados(_ler("contratados", "087", 2024), Counter())
        assert contratados and all(c["chaveNegociante"] in contratos for c in contratados)
