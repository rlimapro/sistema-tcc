"""Orquestração do ETL 2: lê cada arquivo pedido, transforma e grava, contando tudo o que descarta.

Ordem: Empresas, Estabelecimentos, Socios (a `Empresa` de destino já existe antes dos sócios, porque
`garantir_empresas` a cria para toda raiz do grafo no início).

Retomada: ao terminar cada arquivo (fora do modo amostra), o nome vai para um checkpoint JSON em
`data/checkpoints/`. Uma nova execução pula os arquivos já feitos, a menos que se peça `refazer`.
O checkpoint guarda também a pasta de dados (caminho absoluto), o tamanho de cada arquivo lido e a impressão
digital das raízes de CNPJ do grafo (quantidade e sha256). Se a pasta ou as raízes mudaram (uma sondagem
sobre um recorte, ou o ETL 1 trouxe fornecedores novos), o checkpoint é ignorado com aviso; se só o tamanho
de um arquivo mudou, aquele arquivo é relido.
Como toda gravação é `MERGE`, refazer um arquivo pela metade não duplica nada.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from neo4j import Driver

from sarc.etl_rfb import carregador as c
from sarc.etl_rfb.leitor import (
    COLUNAS_POR_TIPO,
    N_PARTICOES,
    EstatisticasLeitura,
    em_lotes,
    ler_dominio,
    ler_filtrado,
    localizar_arquivo,
    tipo_do_arquivo,
    todos_os_arquivos,
)
from sarc.etl_rfb.transformador import (
    EstatisticasRFB,
    transformar_empresa,
    transformar_estabelecimento,
    transformar_socio,
)

logger = logging.getLogger(__name__)

ORDEM_TIPOS = ("Empresas", "Estabelecimentos", "Socios")


@dataclass
class ParametrosRFB:
    raiz: Path
    snapshot: str
    arquivos: list[str] = field(default_factory=list)
    max_linhas: int | None = None
    tamanho_lote: int = 50_000
    checkpoint: Path | None = None
    refazer: bool = False

    def arquivos_ordenados(self) -> list[str]:
        pedidos = self.arquivos or [a for t in ORDEM_TIPOS for a in todos_os_arquivos(t)]
        return sorted(set(pedidos), key=lambda a: (ORDEM_TIPOS.index(tipo_do_arquivo(a)), a))


def impressao_da_execucao(raizes: frozenset[str], dados: Path) -> dict[str, Any]:
    """O que precisa ser igual para reaproveitar o checkpoint: a pasta de dados (caminho absoluto, para que
    uma sondagem sobre um recorte nunca marque arquivos como lidos para a carga completa) e a quantidade e o
    sha256 das raízes ordenadas (mudam se o ETL 1 acrescentar ou tirar fornecedores)."""
    return {
        "dados": str(Path(dados).resolve()),
        "n_raizes": len(raizes),
        "hash_raizes": hashlib.sha256("\n".join(sorted(raizes)).encode()).hexdigest(),
    }


def _ler_checkpoint(caminho: Path | None, impressao: dict[str, Any]) -> dict[str, int]:
    """Arquivos concluídos -> tamanho em bytes quando foram lidos. Vazio se a impressão não bate."""
    if caminho is None or not caminho.is_file():
        return {}
    conteudo = json.loads(caminho.read_text(encoding="utf-8"))
    if {k: conteudo.get(k) for k in impressao} != impressao:
        logger.warning(
            "checkpoint %s ignorado: a pasta de dados ou as raízes de CNPJ do grafo mudaram desde a última "
            "execução (pasta %s, %s raízes antes; pasta %s, %d agora); todos os arquivos serão lidos de novo",
            caminho,
            conteudo.get("dados"),
            conteudo.get("n_raizes"),
            impressao["dados"],
            impressao["n_raizes"],
        )
        return {}
    concluidos = conteudo.get("concluidos", {})
    return concluidos if isinstance(concluidos, dict) else {}


def _gravar_checkpoint(caminho: Path, concluidos: dict[str, int], impressao: dict[str, Any]) -> None:
    caminho.parent.mkdir(parents=True, exist_ok=True)
    temporario = caminho.with_suffix(".tmp")
    conteudo = {"concluidos": dict(sorted(concluidos.items())), **impressao}
    temporario.write_text(json.dumps(conteudo, indent=1), encoding="utf-8")
    os.replace(temporario, caminho)


def _processar_lote(
    driver: Driver,
    tipo: str,
    lote: list[dict[str, str]],
    p: ParametrosRFB,
    cnpjs: frozenset[str],
    municipios: dict[str, str],
    stats: EstatisticasRFB,
    contagens: Counter[str],
) -> None:
    def gravar(cypher: Any, linhas: list[dict[str, Any]], nome: str) -> None:
        contagens[nome] += c.gravar(driver, cypher, linhas, p.snapshot, p.tamanho_lote)

    if tipo == "Empresas":
        gravar(c.CYPHER_EMPRESA, [transformar_empresa(r) for r in lote], "empresas_enriquecidas")
    elif tipo == "Estabelecimentos":
        estabs = [e for r in lote if (e := transformar_estabelecimento(r, cnpjs, municipios, stats))]
        gravar(c.CYPHER_ESTABELECIMENTO, estabs, "estabelecimentos_enriquecidos")
        tels = [{"cnpj": e["cnpj"], "numero": t} for e in estabs for t in e["telefones"]]
        emails = [{"cnpj": e["cnpj"], "endereco": m} for e in estabs for m in e["emails"]]
        ends = [{"cnpj": e["cnpj"], "endereco": e["endereco"]} for e in estabs if e["endereco"]]
        gravar(c.CYPHER_TELEFONE, tels, "tem_telefone")
        gravar(c.CYPHER_EMAIL, emails, "tem_email")
        gravar(c.CYPHER_ENDERECO, ends, "localizado_em")
    else:
        socios = [transformar_socio(r, stats) for r in lote]
        gravar(c.CYPHER_SOCIO, [s["socio"] for s in socios if s["socio"]], "socio")
        gravar(c.CYPHER_SOCIO_PJ, [s["socio_pj"] for s in socios if s["socio_pj"]], "socio_pj")
        gravar(c.CYPHER_REPRESENTA, [s["representa"] for s in socios if s["representa"]], "representa")


def executar(driver: Driver, p: ParametrosRFB) -> dict[str, Any]:
    """Roda o ETL 2 e grava o nó `:Carga`. Devolve as contagens."""
    from sarc.grafo.carga import registrar_carga

    inicio = datetime.now(UTC)
    raizes, cnpjs = c.buscar_cnpjs_conhecidos(driver)
    logger.info("CNPJs no grafo: %d raízes, %d estabelecimentos", len(raizes), len(cnpjs))
    contagens: Counter[str] = Counter()
    contagens["empresas_do_grafo"] = c.garantir_empresas(driver)

    arquivos = p.arquivos_ordenados()
    usar_checkpoint = p.checkpoint is not None and p.max_linhas is None
    impressao = impressao_da_execucao(raizes, p.raiz)
    concluidos = {} if p.refazer or not usar_checkpoint else _ler_checkpoint(p.checkpoint, impressao)
    municipios = ler_dominio(p.raiz, "Municipios") if any(a.startswith("Estab") for a in arquivos) else {}
    stats = EstatisticasRFB()
    leitura = EstatisticasLeitura()
    # Recorte (--arquivos com menos de 30 arquivos ou --max-linhas) fica `parcial`, nunca `concluida`:
    # o MCP não pode declarar o QSA carregado por inteiro a partir de uma sondagem (F-19).
    completo = len(arquivos) == 3 * N_PARTICOES and p.max_linhas is None
    status = "concluida" if completo else "parcial"
    try:
        for nome in arquivos:
            tipo = tipo_do_arquivo(nome)
            caminho = localizar_arquivo(p.raiz, nome)
            tamanho = caminho.stat().st_size
            if concluidos.get(nome) == tamanho:
                logger.info("%s: já concluído no checkpoint, pulado", nome)
                contagens["arquivos_pulados_checkpoint"] += 1
                continue
            t0, do_arquivo = time.monotonic(), EstatisticasLeitura()
            linhas = ler_filtrado(caminho, COLUNAS_POR_TIPO[tipo], raizes, do_arquivo, p.max_linhas)
            for lote in em_lotes(linhas, p.tamanho_lote):
                _processar_lote(driver, tipo, lote, p, cnpjs, municipios, stats, contagens)
            leitura.somar(do_arquivo)
            logger.info(
                "%s: %d linhas lidas, %d da amostra de CNPJs, %d malformadas, %.1f s; acumulado: %s",
                nome,
                do_arquivo.linhas_lidas,
                do_arquivo.linhas_filtradas,
                do_arquivo.linhas_malformadas,
                time.monotonic() - t0,
                dict(contagens),
            )
            if usar_checkpoint and p.checkpoint is not None:
                concluidos[nome] = tamanho
                _gravar_checkpoint(p.checkpoint, concluidos, impressao)
    except BaseException:
        status = "falhou"
        raise
    finally:
        resultado = {**contagens, **stats.como_dict(), **vars(leitura)}
        logger.info("ETL 2 (%s): %s", status, resultado)
        registrar_carga(
            driver,
            "etl-rfb",
            {
                "snapshot_rfb": p.snapshot,
                "arquivos": arquivos,
                "amostra": p.max_linhas,
                "completo": completo,
            },
            resultado,
            inicio,
            datetime.now(UTC),
            status=status,
        )
    return resultado


def executar_grau_registrado(
    driver: Driver, raiz: Path, snapshot: str, arquivos: list[str] | None, max_linhas: int | None
) -> dict[str, int]:
    """Roda a passada do grau nacional (T14) e grava um nó `:Carga` com etapa `etl-rfb-grau`."""
    from sarc.etl_rfb.grau import executar_grau
    from sarc.grafo.carga import registrar_carga

    inicio = datetime.now(UTC)
    resultado = executar_grau(driver, raiz, snapshot, arquivos, max_linhas)
    registrar_carga(
        driver,
        "etl-rfb-grau",
        {"snapshot_rfb": snapshot, "arquivos": arquivos or "todos", "amostra": max_linhas},
        resultado,
        inicio,
        datetime.now(UTC),
        status="concluida" if resultado["grau_completo"] else "parcial",
    )
    return resultado
