"""Comparadores do vínculo aproximado (D-47), com formas reais da tabela 505."""

from __future__ import annotations

import pytest

from sarc.conluio.aproximado import (
    APROXIMADO,
    ENDERECO_APROXIMADO,
    IGUAL_PARCIAL,
    INDEL1,
    MESMO_PREDIO,
    NONO,
    PISTA,
    PREFIXO,
    SEM_DDD,
    SUBST1,
    TELEFONE_APROXIMADO,
    TELEFONE_DIGITO,
    FamiliaEndereco,
    FamiliaTelefone,
    analisar_endereco,
    comparar_enderecos,
    comparar_telefones,
    forma_telefone,
    padrao_telefone,
)


class TestFormaTelefone:
    @pytest.mark.parametrize(
        ("bruto", "forma"),
        [
            ("(81)3093331", "813093331"),  # cortado em 11 caracteres pela fonte
            ("08130933317", "8130933317"),
            ("00035311600", "35311600"),  # sem DDD
            ("55 85 98888-7777", "85988887777"),
        ],
    )
    def test_formas(self, bruto, forma):
        assert forma_telefone(bruto) == forma

    @pytest.mark.parametrize("bruto", ["(85)8823-14", "(88)3531-28", "0000000000", "123", "", None])
    def test_recusa_fragmento_e_lixo(self, bruto):
        assert forma_telefone(bruto) is None


class TestPadraoTelefone:
    @pytest.mark.parametrize(
        ("a", "b", "padrao"),
        [
            ("813093331", "8130933317", PREFIXO),  # Caucaia: JJMR x Global House
            ("8588887777", "85988887777", NONO),
            ("35311600", "8835311600", SEM_DDD),
            ("813093331", "813093331", IGUAL_PARCIAL),
            ("8534567890", "8534667890", SUBST1),
            ("8534567890", "85345167890", INDEL1),
        ],
    )
    def test_padroes(self, a, b, padrao):
        assert padrao_telefone(a, b) == padrao
        assert padrao_telefone(b, a) == padrao

    def test_exato_completo_nao_e_aproximado(self):
        assert padrao_telefone("8530933317", "8530933317") is None

    def test_oito_digitos_so_casa_como_numero_local(self):
        assert padrao_telefone("85309333", "8530933317") is None
        assert padrao_telefone("30933317", "8530933317") == SEM_DDD


class TestComparaTelefones:
    def _familia(self, *pares):
        familia = FamiliaTelefone()
        for forma, raiz in pares:
            familia.adicionar(forma, raiz)
        return familia

    def test_caso_de_caucaia_e_aproximado(self):
        familia = self._familia(("813093331", "11885338"), ("8130933317", "13611287"))
        v = comparar_telefones({"813093331"}, {"8130933317"}, familia, {})
        assert (v.tipo, v.padrao, v.nivel, v.grau) == (TELEFONE_APROXIMADO, PREFIXO, APROXIMADO, 2)

    def test_numero_difundido_nao_liga(self):
        familia = self._familia(*[("8130933317", f"raiz{i}") for i in range(5)], ("813093331", "x"))
        assert comparar_telefones({"813093331"}, {"8130933317"}, familia, {}) is None

    def test_grau_nacional_do_numero_completo_e_piso(self):
        familia = self._familia(("813093331", "a"), ("8130933317", "b"))
        assert comparar_telefones({"813093331"}, {"8130933317"}, familia, {"8130933317": 7}) is None

    def test_digito_trocado_e_pista(self):
        familia = self._familia(("8534567890", "a"), ("8534667890", "b"))
        v = comparar_telefones({"8534567890"}, {"8534667890"}, familia, {})
        assert (v.tipo, v.nivel) == (TELEFONE_DIGITO, PISTA)

    def test_ultimo_digito_e_vizinhos_trocados_nao_ligam(self):
        familia = self._familia(("8534567890", "a"), ("8534567891", "b"), ("8534567809", "c"))
        assert comparar_telefones({"8534567890"}, {"8534567891"}, familia, {}) is None
        assert comparar_telefones({"8534567890"}, {"8534567809"}, familia, {}) is None

    def test_exato_no_certame_dispensa_o_aproximado(self):
        familia = self._familia(("8530933317", "a"), ("8530933317", "b"))
        assert comparar_telefones({"8530933317", "853093331"}, {"8530933317"}, familia, {}) is None


class TestEndereco:
    def _compara(self, ta, ca, tb, cb, mun_a="FORTALEZA", mun_b="FORTALEZA", extras=()):
        a, b = analisar_endereco(ta, ca, mun_a), analisar_endereco(tb, cb, mun_b)
        familia = FamiliaEndereco()
        familia.adicionar(a, "a")
        familia.adicionar(b, "b")
        for i, (t, c, m) in enumerate(extras):
            familia.adicionar(analisar_endereco(t, c, m), f"x{i}")
        return comparar_enderecos(a, b, familia)

    def test_analise(self):
        e = analisar_endereco("rua zezito gomes, 411 sala 01 altos timbu", "61760000", "Caucaia")
        assert (e.numero, e.nome, e.cep5, e.municipio) == ("411", ("ZEZITO", "GOMES"), "61760", "CAUCAIA")
        assert e.complemento == frozenset({("SALA", "1")})

    def test_digitacao_no_mesmo_cep5(self):
        v = self._compara("RUA PEDRO PEREIRA, 120", "60110000", "R PEDRO PERERA, 120", "60110010")
        assert (v.tipo, v.nivel, v.padrao) == (ENDERECO_APROXIMADO, APROXIMADO, "mesmo_cep5")

    def test_cep_generico_x_especifico_no_mesmo_municipio(self):
        """Só o município coincide: exige nome quase idêntico (0,97)."""
        v = self._compara("RUA DR JOSE LOURENCO, 870", "60000000", "R JOSE LOURENCO, 870", "60115280")
        assert (v.tipo, v.padrao) == (ENDERECO_APROXIMADO, "mesmo_municipio")
        assert self._compara("RUA BENEVOLO, 120", "60000000", "R BNEVOLO, 120", "60110000") is None

    def test_ruas_diferentes_nao_ligam(self):
        a, b = "RUA ARMANDO DA COSTA BRITO, 10", "RUA SAO BENEDITO, 10"
        assert self._compara(a, "55590000", b, "55590000") is None

    def test_numero_diferente_nao_liga(self):
        assert self._compara("RUA BENEVOLO, 120", "60110000", "RUA BENEVOLO, 121", "60110000") is None

    def test_sem_numero_nao_liga(self):
        assert self._compara("RUA BENEVOLO, S/N", "60110000", "RUA BENEVOLO S/N", "60110000") is None

    def test_sala_diferente_e_pista(self):
        a, b = "AV SANTOS DUMONT, 1500 SALA 01", "AV SANTOS DUMONT, 1500, SALA 02"
        v = self._compara(a, "60150160", b, "60150161")
        assert (v.tipo, v.nivel) == (MESMO_PREDIO, PISTA)

    def test_numero_de_preenchimento_em_localidade_nao_liga(self):
        assert self._compara("SAO COSMO, 1", "62900000", "VILA SAO COSMO, 01", "62905000", "X", "X") is None

    def test_predio_com_muitas_empresas_nao_liga(self):
        a = analisar_endereco("RUA PEDRO PEREIRA, 120", "60110000", "FORTALEZA")
        b = analisar_endereco("R PEDRO PERERA, 120", "60110010", "FORTALEZA")
        familia = FamiliaEndereco()
        for i in range(5):
            familia.adicionar(a, f"r{i}")
        assert comparar_enderecos(a, b, familia) is None

    def test_grau_nacional_da_chave_exata_e_piso(self):
        a = analisar_endereco("RUA PEDRO PEREIRA, 120", "60110000", "FORTALEZA")
        b = analisar_endereco("R PEDRO PERERA, 120", "60110010", "FORTALEZA")
        assert comparar_enderecos(a, b, FamiliaEndereco(), grau_exato=9) is None
