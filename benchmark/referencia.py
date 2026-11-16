"""Benchmark no formato do trabalho de referência (D-43): `sarc benchmark-referencia`.

Espelha van Erven et al., "Banco de Dados em Grafo: Um Estudo de Caso em Detecção de Fraudes no Governo
Brasileiro" (CISTI 2017): a pergunta dos auditores da CGU "como duas empresas se relacionam a partir de
suas sociedades até o terceiro nível de empresa?", isto é, um caminho empresa – sócio – empresa – sócio –
empresa – sócio – empresa de até 6 saltos pelas arestas societárias (SOCIO e SOCIO_PJ), sem direção e sem
filtro de data.

Para cada par de empresas que disputaram a mesma licitação, quatro formas de responder:

- `neo4j_todas`: padrão de caminho `*1..6` que enumera todas as rotas (a consulta que, no artigo, rodou 14 h
  sem terminar);
- `neo4j_menor`: `allShortestPaths` (a que, no artigo, respondeu em 369 ms);
- `pg_todas`: CTE recursiva que enumera todas as rotas simples (o equivalente das 8 consultas com 12 junções
  que, no artigo, levaram cerca de 20 h no MySQL);
- `pg_menor`: CTE recursiva nível a nível (busca em largura), só com a menor distância.

Rotas são caminhos simples (nenhum nó repetido) nos quatro casos, para as contagens serem comparáveis. Cada
consulta tem um limite de tempo; o que estoura é registrado como "não terminou", como no artigo.
"""

from __future__ import annotations

import json
import logging
import platform
import random
import statistics
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import click
import psycopg
from neo4j import Driver
from psycopg.rows import dict_row

from benchmark import dados
from benchmark.cli import DSN_PADRAO, _ambiente
from sarc.config import Contexto
from sarc.grafo.conexao import abrir_driver, ler

logger = logging.getLogger(__name__)

# Números da Tabela I e II do artigo de referência, para a comparação de escala no relatório.
REFERENCIA = {
    "empresas": 7_754_989,
    "socios": 14_190_151,
    "sociedades": 20_903_480,
    "mysql_s": 72_608.949,
    "neo4j_padrao": "14 h sem terminar (estouro de memória)",
    "neo4j_menor_ms": 369,
}

VARIANTES = ("neo4j_menor", "pg_menor", "pg_todas", "neo4j_todas")  # das rápidas para as lentas

CYPHER: dict[str, str] = {
    "neo4j_todas": """
MATCH (a:Empresa {cnpjBasico: $a}), (b:Empresa {cnpjBasico: $b})
MATCH p = (a)-[:SOCIO|SOCIO_PJ*1..6]-(b)
WHERE all(n IN nodes(p) WHERE single(m IN nodes(p) WHERE m = n))
RETURN count(p) AS rotas, min(length(p)) AS menor
""",
    "neo4j_menor": """
MATCH (a:Empresa {cnpjBasico: $a}), (b:Empresa {cnpjBasico: $b})
OPTIONAL MATCH p = allShortestPaths((a)-[:SOCIO|SOCIO_PJ*..6]-(b))
RETURN count(p) AS rotas, min(length(p)) AS menor
""",
}

SQL: dict[str, str] = {
    "pg_todas": """
WITH RECURSIVE busca (no, visitados, saltos) AS (
  SELECT 'E' || %(a)s, ARRAY['E' || %(a)s], 0
  UNION ALL
  SELECT r.destino_no, b.visitados || r.destino_no, b.saltos + 1
  FROM busca b
  JOIN aresta_societaria r ON r.origem_no = b.no
  WHERE b.saltos < 6 AND b.no <> 'E' || %(b)s AND r.destino_no <> ALL (b.visitados)
)
SELECT count(*) AS rotas, min(saltos) AS menor FROM busca WHERE no = 'E' || %(b)s
""",
    "pg_menor": """
WITH RECURSIVE nivel (no, saltos) AS (
  SELECT 'E' || %(a)s, 0
  UNION
  SELECT r.destino_no, n.saltos + 1
  FROM nivel n
  JOIN aresta_societaria r ON r.origem_no = n.no
  WHERE n.saltos < 6 AND n.no <> 'E' || %(b)s
)
SELECT NULL::bigint AS rotas, min(saltos) AS menor FROM nivel WHERE no = 'E' || %(b)s
""",
}

# Empresas com ao menos uma aresta societária, com o grau (nº de arestas SOCIO|SOCIO_PJ).
CYPHER_GRAUS = """
MATCH (a:Empresa)
WITH a, COUNT { (a)-[:SOCIO|SOCIO_PJ]-() } AS grau
WHERE grau >= 1 AND EXISTS { (a)<-[:PERTENCE_A]-(:Estabelecimento)-[:PARTICIPOU_DE]->(:Licitacao) }
RETURN a.cnpjBasico AS cnpj, grau
"""

# Coparticipantes de uma empresa (outra empresa, com aresta societária, na mesma licitação).
CYPHER_COPARTICIPANTES = """
MATCH (a:Empresa {cnpjBasico: $a})<-[:PERTENCE_A]-(:Estabelecimento)-[:PARTICIPOU_DE]->(l:Licitacao)
      <-[:PARTICIPOU_DE]-(:Estabelecimento)-[:PERTENCE_A]->(b:Empresa)
WHERE b <> a AND EXISTS { (b)-[:SOCIO|SOCIO_PJ]-() }
RETURN DISTINCT b.cnpjBasico AS cnpj, COUNT { (b)-[:SOCIO|SOCIO_PJ]-() } AS grau
"""


@dataclass
class Par:
    a: str
    b: str
    grau_a: int
    grau_b: int
    origem: str  # "grau" (maiores graus) ou "sorteio"


@dataclass
class Resultado:
    variante: str
    tempos: list[float] = field(default_factory=list)
    rotas: int | None = None
    menor: int | None = None
    erro: str | None = None

    @property
    def mediana(self) -> float | None:
        return statistics.median(self.tempos) if self.tempos else None


def escolher_pares(
    graus: dict[str, int],
    coparticipantes: Any,
    n_grau: int,
    n_sorteio: int,
    semente: int,
) -> list[Par]:
    """`n_grau` pares de maior grau mínimo e `n_sorteio` pares sorteados (semente fixa), sem repetir empresa.

    `coparticipantes(cnpj)` devolve [(cnpj, grau)] das empresas que disputaram licitação com `cnpj`.
    """
    pares: list[Par] = []
    usados: set[str] = set()
    for cnpj in sorted(graus, key=lambda c: (-graus[c], c)):
        if len([p for p in pares if p.origem == "grau"]) >= n_grau:
            break
        if cnpj in usados:
            continue
        candidatos = [(c, g) for c, g in coparticipantes(cnpj) if c not in usados and c != cnpj]
        if not candidatos:
            continue
        outro, grau = max(candidatos, key=lambda x: (x[1], x[0]))
        pares.append(Par(cnpj, outro, graus[cnpj], grau, "grau"))
        usados |= {cnpj, outro}
    sorteio = random.Random(semente)
    restantes = sorted(c for c in graus if c not in usados)
    sorteio.shuffle(restantes)
    for cnpj in restantes:
        if len([p for p in pares if p.origem == "sorteio"]) >= n_sorteio:
            break
        if cnpj in usados:
            continue
        candidatos = sorted(c for c, _ in coparticipantes(cnpj) if c not in usados and c != cnpj)
        if not candidatos:
            continue
        outro = sorteio.choice(candidatos)
        pares.append(Par(cnpj, outro, graus[cnpj], graus.get(outro, 0), "sorteio"))
        usados |= {cnpj, outro}
    return pares


def _executar_neo4j(driver: Driver, variante: str, par: Par, timeout: float) -> dict[str, Any]:
    return ler(driver, CYPHER[variante], {"a": par.a, "b": par.b}, timeout=timeout)[0]


def _executar_pg(conexao: psycopg.Connection, variante: str, par: Par) -> dict[str, Any]:
    try:
        with conexao.cursor(row_factory=dict_row) as cursor:
            cursor.execute(SQL[variante], {"a": par.a, "b": par.b})
            linha = cursor.fetchone()
            assert linha is not None
            return dict(linha)
    finally:
        conexao.rollback()


def medir_par(
    driver: Driver, conexao: psycopg.Connection, par: Par, timeout: float, repeticoes: int, rapido: float
) -> list[Resultado]:
    """Cada variante uma vez; se terminou em menos de `rapido` s, mais `repeticoes - 1` vezes (mediana)."""
    resultados = []
    for variante in VARIANTES:
        resultado = Resultado(variante)

        def executar(v: str = variante) -> dict[str, Any]:
            if v.startswith("neo4j"):
                return _executar_neo4j(driver, v, par, timeout)
            return _executar_pg(conexao, v, par)

        try:
            for i in range(repeticoes):
                inicio = time.perf_counter()
                linha = executar()
                resultado.tempos.append(time.perf_counter() - inicio)
                resultado.rotas = linha["rotas"]
                resultado.menor = linha["menor"]
                if i == 0 and resultado.tempos[0] >= rapido:
                    break
        except Exception as erro:  # noqa: BLE001 - timeout, falta de memória: vira linha do relatório
            resultado.erro = _descrever_erro(erro, timeout)
            logger.warning("%s %s-%s: %s", variante, par.a, par.b, resultado.erro)
            _recuperar(driver, conexao)
        logger.info(
            "%s %s-%s: mediana %s s, rotas %s, menor %s",
            variante,
            par.a,
            par.b,
            f"{resultado.mediana:.3f}" if resultado.mediana is not None else "-",
            resultado.rotas,
            resultado.menor,
        )
        resultados.append(resultado)
    return resultados


def _descrever_erro(erro: Exception, timeout: float) -> str:
    texto = f"{type(erro).__name__}: {erro}"
    minusculo = texto.lower()
    if (
        "timeout" in minusculo
        or "timed out" in minusculo
        or "timedout" in minusculo
        or "canceling statement" in minusculo
    ):
        return f"não terminou em {timeout:.0f} s"
    if "memory" in minusculo:
        return "falta de memória"
    return texto[:200]


def _recuperar(driver: Driver, conexao: psycopg.Connection) -> None:
    """Depois de um erro, espera os dois bancos voltarem a responder (o Neo4j pode ter reiniciado)."""
    try:
        conexao.rollback()
    except Exception:  # noqa: BLE001
        pass
    prazo = time.monotonic() + 300
    while time.monotonic() < prazo:
        try:
            ler(driver, "RETURN 1 AS um", timeout=10)
            return
        except Exception:  # noqa: BLE001
            time.sleep(5)


def conferir(resultados: list[Resultado]) -> str:
    """'sim' se as variantes que terminaram concordam (menor distância em todas; nº de rotas entre as duas
    que enumeram todas as rotas); senão descreve a divergência."""
    ok = {r.variante: r for r in resultados if r.erro is None}
    menores = {r.menor for r in ok.values()}
    if len(menores) > 1:
        return "divergente: menor " + ", ".join(f"{v}={r.menor}" for v, r in ok.items())
    if "neo4j_todas" in ok and "pg_todas" in ok and ok["neo4j_todas"].rotas != ok["pg_todas"].rotas:
        return f"divergente: rotas neo4j={ok['neo4j_todas'].rotas} pg={ok['pg_todas'].rotas}"
    return "sim" if len(ok) > 1 else "-"


def _tempo(r: Resultado) -> str:
    if r.erro:
        return r.erro
    m = r.mediana
    assert m is not None
    return f"{m * 1000:.0f} ms" if m < 1 else f"{m:.2f} s".replace(".", ",")


def gerar_relatorio(
    pares: list[Par],
    medidas: list[list[Resultado]],
    volumes: dict[str, int],
    ambiente: dict[str, str],
    parametros: dict[str, Any],
    inicio: datetime,
) -> str:
    linhas: list[str] = []
    add = linhas.append
    p = parametros
    pct_emp = f"{100 * volumes['empresas'] / REFERENCIA['empresas']:.1f}".replace(".", ",")
    pct_soc = f"{100 * volumes['sociedades'] / REFERENCIA['sociedades']:.1f}".replace(".", ",")
    add("# Benchmark no formato do trabalho de referência (Neo4j × PostgreSQL)")
    add("")
    add(f"Gerado por `sarc benchmark-referencia` em {inicio:%d/%m/%Y %H:%M} (D-43). Não edite à mão.")
    add("")
    add(
        'Referência: G. van Erven et al., "Banco de Dados em Grafo: Um Estudo de Caso em Detecção '
        'de Fraudes no Governo Brasileiro", CISTI 2017. Pergunta dos auditores da CGU: **como duas '
        "empresas se relacionam a partir de suas sociedades até o terceiro nível de empresa?** "
        "(empresa – sócio – empresa – sócio – empresa – sócio – empresa: até **6 saltos** pelas "
        "arestas societárias, sem direção e sem filtro de data)."
    )
    add("")
    add("## Escala da base: aqui × artigo")
    add("")
    add("| | este benchmark (grafo do SARC-CE) | artigo (base da CGU) |")
    add("|---|---:|---:|")
    for rotulo, nosso, deles in (
        ("empresas", volumes["empresas"], REFERENCIA["empresas"]),
        ("sócios pessoa física", volumes["pessoas"], REFERENCIA["socios"]),
        ("vínculos societários (SOCIO + SOCIO_PJ)", volumes["sociedades"], REFERENCIA["sociedades"]),
    ):
        add(f"| {rotulo} | {nosso:,} | {deles:,} |".replace(",", "."))
    add("")
    add(
        f"**A base aqui é muito menor que a do artigo:** {pct_emp}% das empresas e "
        f"{pct_soc}% dos vínculos societários. O grafo do SARC-CE só tem as empresas que "
        "aparecem nas licitações dos municípios do Ceará e o quadro societário delas; o artigo "
        "usou a base societária nacional da CGU. Numa rede menor e mais esparsa, há muito menos "
        "caminhos de até 6 saltos, então os tempos dos dois bancos tendem a ser muito menores que "
        "os do artigo, e a diferença entre eles pode aparecer em outra escala. Os números abaixo "
        "valem para esta base e não se extrapolam para a base nacional."
    )
    add("")
    add("## Como foi medido")
    add("")
    add(
        f"- **Pares de empresas:** {p['n_grau']} pares das empresas de maior grau societário que "
        "disputaram a mesma licitação (o artigo usou duas empresas com cerca de 400 sócios cada) e "
        f"{p['n_sorteio']} pares sorteados entre as que têm ao menos um vínculo "
        f"(semente {p['semente']})."
    )
    add(
        "- **Quatro formas de responder:** `neo4j_todas` (padrão `*1..6`, todas as rotas: a "
        "consulta que no artigo não terminou); `neo4j_menor` (`allShortestPaths`: a de 369 ms no "
        "artigo); `pg_todas` (CTE recursiva com todas as rotas simples, no lugar das 8 consultas "
        "com 12 junções do MySQL); `pg_menor` (CTE recursiva nível a nível, só a menor distância). "
        "Rotas são caminhos simples (sem nó repetido) nos quatro casos."
    )
    add(
        "- **Tempo:** de parede no cliente Python, com a transação e a leitura do resultado. "
        f'Limite de {p["timeout"]:.0f} s por consulta (o que estoura é "não terminou"). Cada '
        f"consulta roda uma vez; se terminar em menos de {p['rapido']:.0f} s, roda "
        f"{p['repeticoes']} vezes e vale a mediana."
    )
    add(
        "- **Conferência:** as formas que terminam têm de dar a mesma menor distância, e as duas "
        "que enumeram todas as rotas, o mesmo número de rotas."
    )
    add("")
    add("## Resultados por par")
    add("")
    add(
        "| par | origem | grau A | grau B | menor (saltos) | rotas ≤ 6 | menores caminhos | "
        "neo4j_menor | pg_menor | pg_todas | neo4j_todas | resultados iguais |"
    )
    add("|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|")
    for i, (par, resultados) in enumerate(zip(pares, medidas, strict=True), start=1):
        por = {r.variante: r for r in resultados}
        menor = next((r.menor for r in resultados if r.erro is None and r.menor is not None), None)
        rotas = next((por[v].rotas for v in ("pg_todas", "neo4j_todas") if por[v].erro is None), None)
        caminhos = por["neo4j_menor"].rotas if por["neo4j_menor"].erro is None else None
        celulas = [
            str(i),
            par.origem,
            str(par.grau_a),
            str(par.grau_b),
            str(menor) if menor is not None else "sem vínculo",
            str(rotas) if rotas is not None else "-",
            str(caminhos) if caminhos is not None else "-",
            *(_tempo(por[v]) for v in ("neo4j_menor", "pg_menor", "pg_todas", "neo4j_todas")),
            conferir(resultados),
        ]
        add("| " + " | ".join(celulas) + " |")
    add("")
    add("## Resumo (mediana dos pares que terminaram)")
    add("")
    add(
        "Os pares de maior grau são os comparáveis ao artigo (empresas com muitos sócios); nos "
        "sorteados, de grau baixo, tudo leva poucos milissegundos e o custo fixo do cliente domina."
    )
    add("")
    add("| forma de consulta | pares de maior grau | pares sorteados | todos os pares | artigo |")
    add("|---|---:|---:|---:|---|")
    artigo = {
        "neo4j_menor": f"{REFERENCIA['neo4j_menor_ms']} ms",
        "pg_menor": "-",
        "pg_todas": "≈ 20 h (MySQL, 12 junções)",
        "neo4j_todas": REFERENCIA["neo4j_padrao"],
    }

    def celula(variante: str, origem: str | None) -> str:
        tempos = [
            r.mediana
            for par, rs in zip(pares, medidas, strict=True)
            if origem is None or par.origem == origem
            for r in rs
            if r.variante == variante and r.mediana is not None
        ]
        total = sum(1 for par in pares if origem is None or par.origem == origem)
        if not tempos:
            return f"0 de {total} terminaram"
        texto = f"{statistics.median(tempos) * 1000:.0f} ms (máx. {max(tempos) * 1000:.0f} ms)"
        return texto if len(tempos) == total else f"{texto}; {len(tempos)} de {total} terminaram"

    for variante in ("neo4j_menor", "pg_menor", "pg_todas", "neo4j_todas"):
        add(
            f"| {variante} | {celula(variante, 'grau')} | {celula(variante, 'sorteio')} | "
            f"{celula(variante, None)} | {artigo[variante]} |"
        )
    add("")
    add("## Ambiente")
    add("")
    for chave, valor in ambiente.items():
        add(f"- {chave}: {valor}")
    add("")
    add("## Ameaças à validade")
    add("")
    add(
        f"- **Escala:** a base tem {pct_emp}% das empresas e {pct_soc}% dos vínculos do "
        "artigo (tabela acima); o resultado não se estende à base nacional."
    )
    add(
        "- **Máquina única e compartilhada,** com os dois bancos ao mesmo tempo; o artigo usou uma "
        "máquina com os bancos alternados."
    )
    add(
        "- **SGBD e versões diferentes do artigo:** PostgreSQL 16 com CTE recursiva no lugar do "
        "MySQL 5.1 com junções fixas; Neo4j 5.26 no lugar do 2.1. A CTE recursiva é a forma "
        "idiomática atual de percorrer caminhos de tamanho variável em SQL."
    )
    add(
        "- **Cache:** medidas com cache quente; o custo fixo do driver do Neo4j (alguns ms por "
        "consulta, Q0 em `docs/BENCHMARK.md`) pesa nas consultas rápidas."
    )
    add("- **Seleção dos pares:** por grau e por sorteio com semente fixa, declarada acima.")
    add("")
    return "\n".join(linhas)


@click.command(name="benchmark-referencia")
@click.option("--pg-dsn", default=DSN_PADRAO, show_default=True, help="PostgreSQL do perfil bench.")
@click.option(
    "--pares-grau", default=10, show_default=True, help="Pares das empresas de maior grau societário."
)
@click.option("--pares-sorteio", default=5, show_default=True, help="Pares sorteados.")
@click.option("--semente", default=42, show_default=True, help="Semente do sorteio.")
@click.option("--timeout", default=300.0, show_default=True, help="Limite por consulta, em segundos.")
@click.option("--repeticoes", default=5, show_default=True, help="Execuções das consultas rápidas (mediana).")
@click.option("--rapido", default=10.0, show_default=True, help="Abaixo disto (s), a consulta é repetida.")
@click.option("--sem-recarga", is_flag=True, help="Reaproveita as tabelas já carregadas no PostgreSQL.")
@click.option(
    "--saida",
    type=click.Path(dir_okay=False, path_type=Path),
    default=Path("docs/BENCHMARK-REFERENCIA.md"),
    show_default=True,
)
@click.option(
    "--pasta-dados",
    type=click.Path(file_okay=False, path_type=Path),
    default=Path("benchmark/saida/referencia"),
    show_default=True,
)
@click.pass_obj
def comando(
    obj: Contexto,
    pg_dsn: str,
    pares_grau: int,
    pares_sorteio: int,
    semente: int,
    timeout: float,
    repeticoes: int,
    rapido: float,
    sem_recarga: bool,
    saida: Path,
    pasta_dados: Path,
) -> None:
    """Benchmark no formato de van Erven et al. (2017): vínculo societário de até 6 saltos entre empresas."""
    config = obj.config
    inicio = datetime.now().astimezone()
    with abrir_driver(config) as driver, psycopg.connect(pg_dsn) as conexao:
        if not sem_recarga:
            dados.exportar_csv(driver, pasta_dados / "csv")
            dados.carregar_postgres(conexao, pasta_dados / "csv")
        conexao.execute(f"SET statement_timeout = {int(timeout * 1000)}")
        conexao.commit()
        volumes = {
            "empresas": ler(driver, "MATCH (n:Empresa) RETURN count(n) AS n")[0]["n"],
            "pessoas": ler(driver, "MATCH (n:Pessoa) RETURN count(n) AS n")[0]["n"],
            "sociedades": ler(driver, "MATCH ()-[r:SOCIO|SOCIO_PJ]->() RETURN count(r) AS n")[0]["n"],
        }
        graus = {linha["cnpj"]: linha["grau"] for linha in ler(driver, CYPHER_GRAUS, timeout=900)}
        logger.info("empresas com vínculo societário e participação em licitação: %d", len(graus))

        def coparticipantes(cnpj: str) -> list[tuple[str, int]]:
            return [
                (c["cnpj"], c["grau"]) for c in ler(driver, CYPHER_COPARTICIPANTES, {"a": cnpj}, timeout=300)
            ]

        pares = escolher_pares(graus, coparticipantes, pares_grau, pares_sorteio, semente)
        logger.info("pares escolhidos: %d", len(pares))
        ambiente = _ambiente(driver, conexao)
        medidas = []
        for i, par in enumerate(pares, start=1):
            logger.info(
                "par %d de %d: %s (grau %d) × %s (grau %d)",
                i,
                len(pares),
                par.a,
                par.grau_a,
                par.b,
                par.grau_b,
            )
            medidas.append(medir_par(driver, conexao, par, timeout, repeticoes, rapido))
    ambiente["Python"] = platform.python_version()
    parametros = {
        "n_grau": pares_grau,
        "n_sorteio": pares_sorteio,
        "semente": semente,
        "timeout": timeout,
        "repeticoes": repeticoes,
        "rapido": rapido,
    }
    texto = gerar_relatorio(pares, medidas, volumes, ambiente, parametros, inicio)
    saida.parent.mkdir(parents=True, exist_ok=True)
    saida.write_text(texto, encoding="utf-8")
    pasta_dados.mkdir(parents=True, exist_ok=True)
    (pasta_dados / "resultados.json").write_text(
        json.dumps(
            {
                "inicio": inicio.isoformat(),
                "volumes": volumes,
                "parametros": parametros,
                "pares": [asdict(p) for p in pares],
                "medidas": [[{**asdict(r), "mediana": r.mediana} for r in rs] for rs in medidas],
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    click.echo(f"relatório gravado em {saida}")
    divergentes = [i for i, rs in enumerate(medidas, start=1) if conferir(rs).startswith("divergente")]
    if divergentes:
        click.echo(f"resultado divergente nos pares: {divergentes}", err=True)
        raise SystemExit(1)
