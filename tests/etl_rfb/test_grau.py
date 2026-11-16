"""Grau nacional de telefone, e-mail e endereço (T14)."""

from __future__ import annotations

from pathlib import Path

import pytest
from neo4j import Driver

from sarc.etl_rfb.grau import contar_graus
from sarc.etl_rfb.leitor import localizar_arquivo
from sarc.etl_rfb.pipeline import ParametrosRFB, executar, executar_grau_registrado
from sarc.grafo.chaves import chave_endereco
from sarc.grafo.conexao import escrever, ler

FIXTURES = Path(__file__).parents[1] / "fixtures" / "rfb" / "2026-08"


def test_filiais_da_mesma_empresa_contam_uma_raiz() -> None:
    caminho = localizar_arquivo(FIXTURES, "Estabelecimentos0")
    tel, email, stats, _ = contar_graus(
        [caminho], frozenset({"1123034290", "1141533370"}), frozenset({"info@snfbrasil.com"})
    )
    assert stats == {"linhas_lidas": 9, "linhas_malformadas": 0}
    assert len(tel["1123034290"].raizes) == 1 and tel["1123034290"].estabelecimentos == 4
    assert tel["1141533370"].raizes == {"04942400"}  # DDD "0011" na RFB
    assert len(email["info@snfbrasil.com"].raizes) == 1
    assert "2122842990" not in tel  # contato fora do grafo não é contado


def test_contato_compartilhado_por_empresas_distintas(tmp_path: Path) -> None:
    linhas = [
        '"11111111";"0001";"91";"1";'
        + '"";' * 17
        + '"85";"32345678";"";"";"";"";"ESCRITORIO@CONTAB.COM";"";""',
        '"22222222";"0001";"91";"1";'
        + '"";' * 17
        + '"085";"32345678";"";"";"";"";"escritorio@contab.com";"";""',
        '"22222222";"0002";"91";"2";' + '"";' * 17 + '"85";"32345678";"";"";"";"";"";"";""',
    ]
    pasta = tmp_path / "Estabelecimentos0"
    pasta.mkdir()
    (pasta / "ESTABELE").write_text("\n".join(linhas) + "\n", encoding="latin-1")
    tel, email, stats, _ = contar_graus(
        [pasta / "ESTABELE"], frozenset({"8532345678"}), frozenset({"escritorio@contab.com"})
    )
    assert stats["linhas_malformadas"] == 0
    assert len(tel["8532345678"].raizes) == 2 and tel["8532345678"].estabelecimentos == 3
    assert len(email["escritorio@contab.com"].raizes) == 2


@pytest.mark.integration
def test_grau_gravado_no_grafo(neo4j_teste: Driver) -> None:
    escrever(
        neo4j_teste,
        "UNWIND $c AS c MERGE (e:Estabelecimento {cnpj: c}) SET e.cnpjBasico = substring(c, 0, 8)",
        {"c": ["00934286000182", "04942400000177"]},
    )
    executar(neo4j_teste, ParametrosRFB(raiz=FIXTURES, snapshot="2026-08", arquivos=["Estabelecimentos0"]))
    resultado = executar_grau_registrado(neo4j_teste, FIXTURES, "2026-08", ["Estabelecimentos0"], None)
    assert resultado["telefones_com_grau"] == 2 and resultado["grau_completo"] == 0
    assert ler(neo4j_teste, "MATCH (c:Carga {etapa: 'etl-rfb-grau'}) RETURN c.status AS s") == [
        {"s": "parcial"}
    ]
    graus = ler(
        neo4j_teste,
        "MATCH (t:Telefone) RETURN t.numero AS n, t.grauNacional AS g, t.grauNacionalEstabelecimentos AS e, "
        "t.grauNacionalCompleto AS c ORDER BY n",
    )
    assert graus == [
        {"n": "1123034290", "g": 1, "e": 4, "c": False},
        {"n": "1141533370", "g": 1, "e": 1, "c": False},
    ]
    assert resultado["enderecos_com_grau"] == 2
    ends = ler(neo4j_teste, "MATCH (a:Endereco) RETURN a.grauNacional AS g, a.grauNacionalCompleto AS c")
    assert ends == [{"g": 1, "c": False}] * 2


def test_predio_comercial_conta_empresas_de_salas_diferentes(tmp_path: Path) -> None:
    def linha(raiz: str, ordem: str, complemento: str) -> str:
        campos = [raiz, ordem, "91", "1"] + [""] * 26
        campos[13:19] = ["AVENIDA", "SANTOS DUMONT", "1380", complemento, "ALDEOTA", "60150160"]
        return ";".join(f'"{c}"' for c in campos)

    pasta = tmp_path / "Estabelecimentos0"
    pasta.mkdir()
    linhas = [
        linha("11111111", "0001", "SALA 101"),
        linha("22222222", "0001", "SALA 1502"),
        linha("22222222", "0002", ""),
    ]
    linhas.append(linha("33333333", "0001", "").replace('"1380"', '"1381"'))  # outro número: não conta
    (pasta / "ESTABELE").write_text("\n".join(linhas) + "\n", encoding="latin-1")
    chave = chave_endereco("60150160", "1380", "AVENIDA SANTOS DUMONT")
    assert chave is not None
    _, _, stats, enderecos = contar_graus(
        [pasta / "ESTABELE"], frozenset(), frozenset(), None, frozenset({chave})
    )
    assert stats["linhas_malformadas"] == 0
    assert enderecos[chave].raizes == {"11111111", "22222222"} and enderecos[chave].estabelecimentos == 3
