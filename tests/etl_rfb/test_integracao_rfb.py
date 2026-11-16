"""ETL 2 de ponta a ponta no Neo4j de teste, com as fixtures reais reduzidas."""

from __future__ import annotations

from pathlib import Path

import pytest
from neo4j import Driver

from sarc.etl_rfb.pipeline import ParametrosRFB, executar
from sarc.grafo.conexao import escrever, ler

pytestmark = pytest.mark.integration

FIXTURES = Path(__file__).parents[1] / "fixtures" / "rfb" / "2026-08"
# Os 4 CNPJs que o "ETL 1" criou. As outras filiais das mesmas raízes estão no CSV e não podem entrar.
CNPJS_DO_GRAFO = ["00085822000112", "00934286000182", "22705562000173", "04942400000177"]

CONTAGEM_GRAFO = """
CALL () { MATCH (n) RETURN count(n) AS nos }
CALL () { MATCH ()-[r]->() RETURN count(r) AS arestas }
CALL () { MATCH (e:Estabelecimento) RETURN count(e) AS estabelecimentos }
RETURN nos, arestas, estabelecimentos
"""


def _semear(driver: Driver) -> None:
    escrever(
        driver,
        "UNWIND $cnpjs AS c MERGE (e:Estabelecimento {cnpj: c}) SET e.cnpjBasico = substring(c, 0, 8)",
        {"cnpjs": CNPJS_DO_GRAFO},
    )


def _rodar(driver: Driver, **extra: object) -> dict:
    return executar(driver, ParametrosRFB(raiz=FIXTURES, snapshot="2026-08", tamanho_lote=3, **extra))  # type: ignore[arg-type]


def _contagem(driver: Driver) -> dict:
    return ler(driver, CONTAGEM_GRAFO)[0]


def test_carga_idempotente_e_modelo(neo4j_teste: Driver) -> None:
    _semear(neo4j_teste)
    primeira = _rodar(neo4j_teste, arquivos=["Socios0", "Socios1", "Empresas0", "Estabelecimentos0"])
    depois_1 = _contagem(neo4j_teste)
    _rodar(neo4j_teste, arquivos=["Socios0", "Socios1", "Empresas0", "Estabelecimentos0"])
    depois_2 = _contagem(neo4j_teste)
    # A 2ª execução cria só o seu nó :Carga.
    assert depois_2["arestas"] == depois_1["arestas"]
    assert depois_2["nos"] == depois_1["nos"] + 1
    assert depois_2["estabelecimentos"] == len(CNPJS_DO_GRAFO)  # nenhum Estabelecimento criado

    assert primeira["socios_estrangeiros_descartados"] == 1
    assert primeira["linhas_malformadas"] == 1
    assert primeira["estabelecimentos_fora_de_escopo"] == 5

    # SOCIO_PJ com data; o documento de 14 dígitos (Socios1) cai na mesma aresta do de 8 (Socios0).
    pj = ler(
        neo4j_teste,
        "MATCH (h:Empresa)-[r:SOCIO_PJ]->(e:Empresa {cnpjBasico: '00085822'}) "
        "RETURN h.cnpjBasico AS holding, "
        "toString(r.dataEntrada) AS data, r.snapshot AS snap, r.fonte AS fonte",
    )
    assert pj == [{"holding": "13261413", "data": "2011-09-06", "snap": "rfb-2026-08", "fonte": "rfb"}]

    # Toda aresta de vínculo e de contato tem snapshot e fonte.
    sem_origem = ler(
        neo4j_teste,
        "MATCH ()-[r:SOCIO|SOCIO_PJ|REPRESENTA|TEM_TELEFONE|TEM_EMAIL|LOCALIZADO_EM]->() "
        "WHERE r.snapshot IS NULL OR r.fonte IS NULL RETURN count(r) AS n",
    )
    assert sem_origem == [{"n": 0}]

    # REPRESENTA sem data; o procurador 17 está no grafo com a qualificação para a consulta filtrar.
    reps = ler(
        neo4j_teste,
        "MATCH (:Pessoa)-[r:REPRESENTA]->(e:Empresa) "
        "RETURN e.cnpjBasico AS emp, r.qualificacao AS q, r.dataEntrada AS d ORDER BY emp",
    )
    assert reps == [
        {"emp": "00085822", "q": "05", "d": None},
        {"emp": "00934286", "q": "17", "d": None},
        {"emp": "04942400", "q": "17", "d": None},
    ]

    # Sócio menor mascarado; nenhum nome de pessoa no grafo.
    quals = ler(neo4j_teste, "MATCH ()-[r:SOCIO]->() RETURN collect(DISTINCT r.qualificacao) AS q")[0]["q"]
    assert "30" not in quals
    props_pessoa = ler(neo4j_teste, "MATCH (p:Pessoa) UNWIND keys(p) AS k RETURN collect(DISTINCT k) AS k")
    assert props_pessoa == [{"k": ["id"]}]

    # Contatos só das filiais do grafo, sem nó órfão.
    orfaos = ler(
        neo4j_teste,
        "MATCH (c) WHERE (c:Telefone OR c:Email OR c:Endereco) AND NOT (c)<--() RETURN count(c) AS n",
    )
    assert orfaos == [{"n": 0}]
    tel = ler(
        neo4j_teste,
        "MATCH (e:Estabelecimento)-[:TEM_TELEFONE]->(t) RETURN e.cnpj AS cnpj, t.numero AS t "
        "ORDER BY cnpj, t",
    )
    assert {"cnpj": "04942400000177", "t": "1141533370"} in tel
    assert all(linha["cnpj"] in CNPJS_DO_GRAFO for linha in tel)

    razao = ler(neo4j_teste, "MATCH (e:Empresa {cnpjBasico: '22705562'}) RETURN e.razaoSocial AS r")
    assert razao == [{"r": "M&M PARTICIPACOES LTDA"}]
    carga = ler(neo4j_teste, "MATCH (c:Carga {etapa: 'etl-rfb'}) RETURN c.snapshotRfb AS s, c.status AS st")
    assert carga == [{"s": "2026-08", "st": "parcial"}] * 2  # 4 de 30 arquivos: recorte


def test_checkpoint_pula_arquivo_concluido(neo4j_teste: Driver, tmp_path: Path) -> None:
    _semear(neo4j_teste)
    checkpoint = tmp_path / "rfb.json"
    _rodar(neo4j_teste, arquivos=["Socios0"], checkpoint=checkpoint)
    segunda = _rodar(neo4j_teste, arquivos=["Socios0"], checkpoint=checkpoint)
    assert segunda["arquivos_pulados_checkpoint"] == 1 and segunda.get("socio", 0) == 0
    terceira = _rodar(neo4j_teste, arquivos=["Socios0"], checkpoint=checkpoint, refazer=True)
    assert terceira["socio"] > 0


def test_sem_estabelecimento_no_grafo_falha(neo4j_teste: Driver) -> None:
    with pytest.raises(RuntimeError, match="etl-tce"):
        _rodar(neo4j_teste, arquivos=["Socios0"])


def test_checkpoint_ignorado_quando_o_etl1_traz_cnpj_novo(neo4j_teste: Driver, tmp_path: Path) -> None:
    _semear(neo4j_teste)
    checkpoint = tmp_path / "rfb.json"
    _rodar(neo4j_teste, arquivos=["Socios0"], checkpoint=checkpoint)
    escrever(neo4j_teste, "MERGE (e:Estabelecimento {cnpj: '11222333000181'}) SET e.cnpjBasico = '11222333'")
    segunda = _rodar(neo4j_teste, arquivos=["Socios0"], checkpoint=checkpoint)
    assert segunda.get("arquivos_pulados_checkpoint", 0) == 0
    assert segunda["socio"] > 0


def test_recorte_grava_status_parcial_e_conta_duplicata(neo4j_teste: Driver) -> None:
    _semear(neo4j_teste)
    resultado = _rodar(neo4j_teste, arquivos=["Socios0", "Socios1"])
    assert resultado["socio_duplicado_na_empresa"] == 1  # holding 13261413 em Socios0 e Socios1
    carga = ler(
        neo4j_teste, "MATCH (c:Carga {etapa: 'etl-rfb'}) RETURN c.parametrosJson AS p, c.status AS st"
    )
    assert '"completo": false' in carga[0]["p"] and carga[0]["st"] == "parcial"


def test_checkpoint_de_um_recorte_nao_vale_para_outra_pasta(neo4j_teste: Driver, tmp_path: Path) -> None:
    import shutil

    _semear(neo4j_teste)
    recorte = tmp_path / "recorte"
    shutil.copytree(FIXTURES, recorte)
    checkpoint = tmp_path / "rfb.json"
    executar(
        neo4j_teste,
        ParametrosRFB(raiz=recorte, snapshot="2026-08", arquivos=["Socios0"], checkpoint=checkpoint),
    )
    completa = _rodar(neo4j_teste, arquivos=["Socios0"], checkpoint=checkpoint)  # pasta FIXTURES, outra
    assert completa.get("arquivos_pulados_checkpoint", 0) == 0 and completa["socio"] > 0
    # Mesmo caminho, arquivo com outro tamanho (snapshot trocado na mesma pasta): também relê.
    arquivo = next((recorte / "Socios0").iterdir())
    executar(
        neo4j_teste,
        ParametrosRFB(raiz=recorte, snapshot="2026-08", arquivos=["Socios0"], checkpoint=checkpoint),
    )
    with open(arquivo, "a", encoding="latin-1") as f:
        f.write(
            '"00085822";"2";"PESSOA FICTICIA J";"***000010**";"22";"20200101";"";"***000000**";"";"00";"5"\n'
        )
    relida = executar(
        neo4j_teste,
        ParametrosRFB(raiz=recorte, snapshot="2026-08", arquivos=["Socios0"], checkpoint=checkpoint),
    )
    assert relida.get("arquivos_pulados_checkpoint", 0) == 0
