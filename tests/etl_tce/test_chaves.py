"""Chaves determinísticas. Os textos de endereço e telefone são transcritos do cache real da tabela 505."""

from __future__ import annotations

import pytest

from sarc.grafo.chaves import (
    _alfanum,
    _dia,
    _n,
    chave_endereco,
    chave_telefone,
    cnpj_dv_confere,
    cnpj_valido,
    gerar_id_pessoa,
    id_contrato,
    id_licitacao,
    id_municipio,
    numero_do_endereco,
    primeira_palavra_do_logradouro,
)


class TestNormalizacao:
    def test_n(self):
        assert _n(None) == ""
        assert _n("  02  ") == "02"
        assert _n("074") == "074"

    def test_dia_corta_hora_e_espaco(self):
        assert _dia("  2026-01-09T00:00:00  ") == "2026-01-09"

    def test_alfanum(self):
        assert _alfanum("16.002.2026PERP") == "160022026PERP"
        assert _alfanum("  08.004-2025.con ") == "080042025CON"
        assert _alfanum(None) == ""

    def test_municipio_preserva_zero_a_esquerda(self):
        assert id_municipio(" 087 ") == "087"


class TestIdLicitacao:
    def test_pontuacao_diferente_casa(self):
        """`numero_processo_adm` (/contratos) e `numero_licitacao` (/processos) vêm pontuados diferente."""
        assert (
            id_licitacao("087", "2603.01-2024-PE")
            == id_licitacao("087", "2603.01/2024-PE")
            == "087|2603012024PE"
        )

    def test_municipio_separa_numeracao_igual(self):
        assert id_licitacao("144", "001/2021") != id_licitacao("087", "001/2021")
        assert id_licitacao("087", "X") != id_licitacao("87", "X")


class TestIdContrato:
    def test_original_sem_sufixo(self):
        assert id_contrato("144", " 20260011 ", "2026-01-09T00:00:00") == "144|20260011|2026-01-09"

    def test_aditivos_do_mesmo_lote_nao_colapsam(self):
        a = id_contrato("144", "2014.03.24-0002", "2014-10-14", "2014.03.24-0015", "2014-03-24T00:00:00")
        b = id_contrato("144", "2014.03.24-0002", "2014-10-14", "2014.03.24-0017", "2014-03-24T00:00:00")
        assert a == "144|2014.03.24-0002|2014-10-14#2014.03.24-0015|2014-03-24"
        assert a != b

    def test_sustenido_no_numero_sobrevive(self):
        """Ibaretama: o '#' faz parte do próprio número do contrato."""
        assert id_contrato("069", "20160108004#", "2016-01-08") == "069|20160108004#|2016-01-08"


def test_id_pessoa_estavel_e_sem_caixa():
    assert gerar_id_pessoa("***123456**", "Fulano") == gerar_id_pessoa("***123456**", "FULANO")
    assert gerar_id_pessoa("***123456**", "FULANO") != gerar_id_pessoa("***123456**", "BELTRANO")


class TestCnpj:
    def test_aceita_14_digitos_inclusive_formatado(self):
        assert cnpj_valido("08458279000163") == "08458279000163"
        assert cnpj_valido(" 07.544.576/0001-69 ") == "07544576000169"

    @pytest.mark.parametrize(
        "documento", ["", None, "   ", "123", "084582790001634", "00000000000000", "AAAA=="]
    )
    def test_recusa_o_resto(self, documento):
        """Um documento vazio viraria o nó `Estabelecimento {cnpj: ''}`, hub falso de conluio."""
        assert cnpj_valido(documento) is None


class TestTelefone:
    @pytest.mark.parametrize(
        ("bruto", "esperado"),
        [
            ("08834120445", "8834120445"),  # zero de tronco
            ("8533511606", "8533511606"),
            ("85986537824", "85986537824"),  # celular com 9
            ("(85) 3351-1606", "8533511606"),
            ("+55 85 98653-7824", "85986537824"),
            ("0085986537824", "85986537824"),
        ],
    )
    def test_normaliza(self, bruto, esperado):
        assert chave_telefone(bruto) == esperado

    @pytest.mark.parametrize(
        "bruto", ["", None, "0", "33511606", "986537824", "8500000000", "123456789012345"]
    )
    def test_sem_ddd_ou_lixo_fica_sem_chave(self, bruto):
        assert chave_telefone(bruto) is None


class TestEndereco:
    @pytest.mark.parametrize(
        ("endereco", "numero"),
        [
            ("rua zezito gomes, 411 sala 01 altos timbu", "411"),
            ("R ZEZITO GOMES N 411    TIMBU", "411"),
            ("AV MONSENHOR TABOSA 1001 LOJA N 05 CENTRO,FORTALEZA,CE, CEP 60165011", "1001"),
            ("AV MONSENHOR TABOSA 1001 LOJA 05 PRAIA DE IRACEMA,FORTALEZA,CE", "1001"),
            ("Rua 31 de Marco, 207    Centro", "207"),
            ("Rua 1 de maio, 2475    granja portugal", "2475"),
            ("AV. DAS AMERICAS, 13685, SALA 0240    BR DA TIJUCA", "13685"),
            ("RUA JOAQUIM RODRIGUES CARNEIRO, N 32", "32"),
            ("RUA BASILIO PINTO No 205  205   CENTRO", "205"),
            ("R JOAO PITOMBEIRA  013   CENTRO", "13"),
            ("RUA X, 123, CENTRO, CEP 62580000", "123"),
            ("RUA X, 123, CENTRO, 62.580-000", "123"),
        ],
    )
    def test_numero_da_porta(self, endereco, numero):
        assert numero_do_endereco(endereco) == numero

    @pytest.mark.parametrize(
        "endereco",
        [
            "RUA PADRE VICENTE JORGE, S/N.    CENTRO",
            "Rua Luiz Taumaturgo Furtado, s/n",
            "RUA MARIA MAIA    CENTRO",
            # CEP no texto não é número da porta (17.400 declarações da 505 caíam nisso)
            "RUA ALMIR DE ALMEIDA CASTRO,CENTRO,CEP61600040",
            ",,CEP63230000,Granjeiro",
            "RUA PEDRO FIRMINO, CENTRO, MILAGRES. CE. 63.250.000",
            "RUA X, CENTRO, CEP: 62.580-000",
            "RUA X 62580000",
            "RUA X, CENTRO, CEP 62580",
            "",
            None,
        ],
    )
    def test_sem_numero(self, endereco):
        assert numero_do_endereco(endereco) is None

    def test_par_do_acordao_de_itatira_casa(self):
        """O par de 1308.02/2021-PE escreve o mesmo endereço de dois jeitos (v2: a chave estrita perdia)."""
        a = "rua zezito gomes, 411 sala 01 altos timbu"
        b = "R ZEZITO GOMES N 411    TIMBU"
        chave_a = chave_endereco("61760000", numero_do_endereco(a), a)
        assert chave_a == chave_endereco("61760000", numero_do_endereco(b), b) == "61760000|411|ZEZITO"

    def test_cep_generico_exige_a_rua(self):
        """CEP terminado em 000 é de uma cidade inteira: mesmo número em ruas diferentes não casa."""
        a = "RUA JOSE SANTOS FILHO, 175 - TERREO"
        b = "R FRANCISCA DE ASSIS UCHOA, 175"
        chave_a = chave_endereco("63870000", numero_do_endereco(a), a)
        chave_b = chave_endereco("63870000", numero_do_endereco(b), b)
        assert chave_a == "63870000|175|JOSE"
        assert chave_a != chave_b

    def test_cep_generico_tolera_titulo_na_rua(self):
        a = "RUA Dr. MOREIRA DA ROCHA, 955 SALA 101"
        b = "RUA MOREIRA DA ROCHA, 955 A, SALA 103"
        assert chave_endereco("63700000", "955", a) == chave_endereco("63700000", "955", b)

    @pytest.mark.parametrize(
        ("cep", "numero"), [("6176000", "411"), ("00000000", "411"), ("", "411"), ("61760000", None)]
    )
    def test_sem_cep_valido_ou_numero_nao_ha_chave(self, cep, numero):
        assert chave_endereco(cep, numero, "RUA X") is None

    def test_numero_estruturado_da_rfb(self):
        assert chave_endereco("61760-000", "0411", "ZEZITO GOMES") == "61760000|411|ZEZITO"

    def test_primeira_palavra(self):
        assert primeira_palavra_do_logradouro("AV. PRESIDENTE GEISEL, 1922") == "GEISEL"
        assert primeira_palavra_do_logradouro("Rua São João") == "JOAO"
        assert primeira_palavra_do_logradouro("TR SANTA LUZIA, 12") == "LUZIA"


def test_digitos_verificadores():
    assert cnpj_dv_confere("08458279000163")
    assert cnpj_dv_confere("27499707000140")
    assert not cnpj_dv_confere("08458279000164")
    assert not cnpj_dv_confere("123")
