"""Gera `docs/BENCHMARK.md` a partir das medidas."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from benchmark.consultas import CONSULTAS
from benchmark.medir import Medida


def _ms(segundos: float | None) -> str:
    if segundos is None:
        return "-"
    return f"{segundos * 1000:.1f}" if segundos < 10 else f"{segundos * 1000:.0f}"


def _menos(m: Medida, base: Medida) -> str:
    if m.mediana is None or base.mediana is None or m.consulta == base.consulta:
        return "-"
    return _ms(max(m.mediana - base.mediana, 0.0))


def _razao(neo: Medida, pg: Medida) -> str:
    if neo.mediana is None or pg.mediana is None or neo.mediana == 0:
        return "-"
    return f"{pg.mediana / neo.mediana:.2f}"


def gerar(
    medidas: dict[tuple[str, str], Medida],
    ambiente: dict[str, Any],
    volumes_grafo: dict[str, int],
    volumes_tabelas: dict[str, int],
    regime: dict[str, Any],
    cargas: list[dict[str, Any]],
    referencia_q1: dict[str, Any],
    parametros: dict[str, Any],
    inicio: datetime,
) -> str:
    linhas: list[str] = []
    add = linhas.append
    add("# Benchmark mínimo Neo4j × PostgreSQL")
    add("")
    add(
        f"Gerado por `sarc benchmark` em {inicio:%d/%m/%Y %H:%M} (horário local). "
        "Não edite à mão: rode de novo."
    )
    add("Método e consultas: CRITICA §2.1 e `benchmark/consultas.py` (Cypher e SQL lado a lado).")
    add("")
    add("## Dados")
    add("")
    add("- Cargas do grafo (nós `:Carga`, da mais recente para a mais antiga):")
    for carga in cargas:
        recorte = ""
        if carga.get("municipios"):
            municipios = ", ".join(carga["municipios"])
            recorte = f"municípios {municipios}, anos {carga.get('desde')}-{carga.get('ate')}"
        elif carga.get("snapshotRfb"):
            arquivos = (carga.get("parametros") or {}).get("arquivos") or []
            if isinstance(arquivos, str):
                arquivos = [arquivos]
            recorte = f"snapshot {carga['snapshotRfb']}"
            if arquivos:
                recorte += f", arquivos {', '.join(arquivos)}"
        amostra = f", amostra {carga['amostra']}" if carga.get("amostra") is not None else ""
        add(f"  - {carga.get('etapa')} ({carga.get('status')}): {recorte}{amostra}")
    if regime.get("cargasNaoConcluidas"):
        add(
            f"  - {regime['cargasNaoConcluidas']} carga(s) não concluída(s); `parcial` no ETL 2 é o recorte "
            "dos CSVs pelas raízes de CNPJ do grafo (`scripts/amostra_ponta_a_ponta.sh`)."
        )
    add(
        f"- Licitação de referência da Q1: `{referencia_q1.get('id')}` "
        f"({referencia_q1.get('n')} participantes)."
    )
    add("")
    add("| Neo4j (nós e arestas) | qtd. | PostgreSQL (tabelas) | linhas |")
    add("|---|---:|---|---:|")
    grafo = list(volumes_grafo.items())
    tabelas = list(volumes_tabelas.items())
    for i in range(max(len(grafo), len(tabelas))):
        g = grafo[i] if i < len(grafo) else ("", "")
        t = tabelas[i] if i < len(tabelas) else ("", "")
        add(f"| {g[0]} | {g[1]} | {t[0]} | {t[1]} |")
    add("")
    add("## Resultados")
    add("")
    add(
        "Tempo de parede no cliente Python, em **milissegundos**, incluindo a transação e a leitura de "
        f"todas as linhas. Cache quente: {parametros['aquecimento']} aquecimentos descartados e "
        f"{parametros['medidas']} medidas. Cache frio: {parametros['frio']} execuções, cada uma logo após "
        "reiniciar os dois containers (o cache de páginas do sistema operacional não é limpo)."
    )
    add("")
    add(
        "| consulta | saltos | linhas | mesmo resultado | Neo4j mediana | Neo4j p95 | PG mediana | PG p95 "
        "| PG/Neo4j | Neo4j − Q0 | PG − Q0 | Neo4j frio | PG frio |"
    )
    add("|---|---|---:|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|")
    base_neo, base_pg = medidas[("Q0", "neo4j")], medidas[("Q0", "postgres")]
    for consulta in CONSULTAS:
        neo, pg = medidas[(consulta.nome, "neo4j")], medidas[(consulta.nome, "postgres")]
        if neo.erro or pg.erro:
            igual = "erro"
        else:
            igual = "sim" if (neo.linhas, neo.impressao) == (pg.linhas, pg.impressao) else "**NÃO**"
        add(
            f"| {consulta.nome} | {consulta.saltos} | {neo.linhas if not neo.erro else '-'} | {igual} "
            f"| {_ms(neo.mediana)} | {_ms(neo.p95)} | {_ms(pg.mediana)} | {_ms(pg.p95)} | {_razao(neo, pg)} "
            f"| {_menos(neo, base_neo)} | {_menos(pg, base_pg)} "
            f"| {_ms(neo.mediana_frio)} | {_ms(pg.mediana_frio)} |"
        )
    add("")
    add("PG/Neo4j > 1: o Neo4j foi mais rápido (mediana quente); < 1: o PostgreSQL foi mais rápido.")
    add(
        '"− Q0": mediana da consulta menos a mediana da Q0 do mesmo banco, isto é, o tempo além do custo '
        "fixo do cliente e da transação (aproximação: as duas medianas não vêm das mesmas execuções)."
    )
    erros = [m for m in medidas.values() if m.erro]
    if erros:
        add("")
        add("Erros:")
        for m in erros:
            add(f"- {m.consulta}/{m.banco}: `{m.erro}`")
    add("")
    add("Hash das linhas (SHA-256, 16 hex, ordem-independente):")
    add("")
    for consulta in CONSULTAS:
        neo, pg = medidas[(consulta.nome, "neo4j")], medidas[(consulta.nome, "postgres")]
        add(f"- {consulta.nome}: Neo4j `{neo.impressao or '-'}`, PostgreSQL `{pg.impressao or '-'}`")
    add("")
    add("## Consultas")
    add("")
    for consulta in CONSULTAS:
        add(f"- **{consulta.nome}** ({consulta.saltos}): {consulta.descricao}.")
    add("")
    add("## Ambiente")
    add("")
    for chave, valor in ambiente.items():
        add(f"- {chave}: {valor}")
    add("")
    add("## Ameaças à validade")
    add("")
    add(
        "- **Volume pequeno.** A amostra tem poucos milhares de nós; em volume pequeno, tudo cabe em memória "
        "e o custo fixo (rede, transação, planejamento) pesa mais que a travessia. O resultado não se "
        "extrapola para a base completa sem nova medição."
    )
    add(
        "- **Máquina única** e compartilhada com outros processos (inclusive os dois bancos ao mesmo tempo). "
        "A carga da máquina no início e no fim está em Ambiente; com load average alto, os tempos absolutos "
        "sobem e a variância também. A Q0 mostra o custo fixo de cada cliente: o driver Python do Neo4j "
        "(Bolt) tem custo por consulta maior que o psycopg, o que pesa nas consultas rápidas."
    )
    add(
        "- **Um só conjunto de dados** e uma só modelagem relacional (a mais direta: uma tabela por relação, "
        "B-tree nas chaves de junção). Outra modelagem ou índices diferentes podem mudar o resultado."
    )
    add(
        "- **Otimizadores e caches.** O Neo4j guarda o plano das consultas repetidas; o psycopg prepara a "
        "consulta no servidor a partir da 5ª execução. O cache frio só reinicia os containers; o cache de "
        "páginas do sistema operacional continua quente. No Neo4j, a primeira consulta depois do reinício "
        "inclui o aquecimento da JVM e o planejamento, por isso o frio dele é muito maior."
    )
    add(
        "- **Memória.** Neo4j com heap de 1-2 GB e page cache de 1 GB; PostgreSQL com shared_buffers de "
        "1 GB e work_mem de 64 MB (`docker-compose.yml`). Nenhum dos dois foi afinado além disso."
    )
    add(
        "- **Equivalência.** As consultas foram escritas para devolver o mesmo conjunto (conferido por "
        "contagem e hash), e não são a consulta de conluio completa do sistema (sem qualificação nem nível)."
    )
    add(
        "- **Q4.** A CTE recursiva enumera caminhos simples até 6 saltos e só depois toma o mínimo; o "
        "`shortestPath` do Neo4j faz busca em largura. É a forma idiomática de cada linguagem, e não o "
        "melhor algoritmo possível em SQL."
    )
    add("")
    return "\n".join(linhas)
