"""Extração da API TCE-CE SIM com cache em disco, no modo cache-primeiro.

Porte limpo de `mvp-v2/pipelines/tce_sim/etl/extractor.py`, só com os 5 endpoints do núcleo
(DECISOES D-02). Layout do cache, o mesmo do v2: `<cache>/<endpoint>/<municipio>/<balde>.json`,
em que o balde é o ano (endpoints com janela de data) ou `0` (`/municipios`).

Regras:
- um balde só é aceito do cache se for **válido** (`ler_cache_valido`): JSON íntegro, lista, e todo
  registro do município e do ano pedidos. Cache inválido é recoletado, nunca usado;
- o balde do ano corrente (ou futuro) nunca vem do cache, porque ainda está crescendo na fonte
  (exceto em `so_cache`, que não usa a rede);
- o cache extra (por exemplo o do v2, DECISOES D-06) é só lido; o arquivo novo vai sempre para o
  cache principal, gravado em `.tmp` e trocado com `os.replace` (nada de arquivo truncado);
- balde que não pôde ser obtido fica registrado como `faltando`, e o relatório final o lista
  (a CLI sai com código diferente de zero). No v2, `--from-cache` pulava o balde com um WARNING.
"""

from __future__ import annotations

import gzip
import json
import logging
import os
import threading
import time
from collections import Counter
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

logger = logging.getLogger(__name__)

MUNICIPIOS = "municipios"
PROCESSOS = "processos_administrativos_contratacoes"
LICITANTES = "licitantes_fornecedores_bens_servicos"
CONTRATOS = "contratos"
CONTRATADOS = "contratados"

# Campo de data que a API usa como janela em cada endpoint: em cada balde do cache do v2, este
# campo cai no ano do balde em 100% dos registros. É o que `ler_cache_valido` confere.
CAMPO_JANELA: dict[str, str] = {
    PROCESSOS: "data_realizacao_autuacao_licitacao",
    LICITANTES: "data_realizacao_licitacao",
    CONTRATOS: "data_contrato",
    CONTRATADOS: "data_contrato",
}
ENDPOINTS_COM_JANELA: tuple[str, ...] = (PROCESSOS, LICITANTES, CONTRATOS, CONTRATADOS)

# O formato da data de janela não é o mesmo em todos os endpoints (mudou na fonte em 10/2026):
# /licitantes exige AAAA-MM-DD (HTTP 400 com mensagem para AAAAMMDD); os outros exigem AAAAMMDD
# e devolvem HTTP 500 para AAAA-MM-DD. O padrão é AAAAMMDD e a exceção é listada: endpoint novo
# que exija hífen erra com 400 e mensagem clara, em vez de um 500 que parece queda da fonte.
ENDPOINTS_DATA_COM_HIFEN = frozenset({LICITANTES})

BALDE_SEM_JANELA = "0"

# ---------------------------------------------------------------------------
# Execução orçamentária (bloco `execucao`, preterição; DECISOES D-36, docs/propostas/ingestao.md)
# ---------------------------------------------------------------------------
# Outro regime de janela: balde = (município, exercício `AAAA00`, mês de REMESSA `AAAAMM`), os três
# obrigatórios. Sem o mês a API devolve outra projeção (sem valores, com CPF em claro): nunca usar.

NOTAS_EMPENHOS = "notas_empenhos"
LIQUIDACOES = "liquidacoes"
NOTAS_PAGAMENTOS = "notas_pagamentos"
PAGAMENTOS_LIQUIDACOES = "pagamentos_liquidacoes"
ENDPOINTS_EXECUCAO: tuple[str, ...] = (NOTAS_EMPENHOS, LIQUIDACOES, NOTAS_PAGAMENTOS, PAGAMENTOS_LIQUIDACOES)

# Sem ordem, a paginação de balde com várias páginas troca registros por repetidos (liquidações e
# pagamentos: no cache do v2, 294 de 573 e 239 de 665 baldes multipágina). Com `$orderby` pela chave
# composta, duas coletas são idênticas e sem repetição nos quatro endpoints: dispensa a saturação do v2.
# `numero_empenho` é sequência por órgão: o órgão entra na chave (revisor-f2 mediu 2 empates sem ele).
ORDENACAO: dict[str, str] = {
    NOTAS_EMPENHOS: "numero_empenho,codigo_orgao,codigo_unidade_orcamentaria,data_emissao_empenho,"
    "estado_empenho,valor_empenhado",
    LIQUIDACOES: "codigo_orgao,numero_empenho,numero_sub_empenho_liquidacao,data_liquidacao,valor_liquidado",
    NOTAS_PAGAMENTOS: "codigo_orgao,numero_empenho,numero_sub_empenho,numero_nota_pagamento,"
    "data_nota_pagamento,valor_nota_pagamento",
    PAGAMENTOS_LIQUIDACOES: "numero_nota_empenho,numero_sub_empenho,numero_nota_pagamento,data_liquidacao,"
    "codigo_orgao",
}

# Meses de remessa por exercício, a partir de janeiro do exercício. Cache do v2: empenhos 100% nos
# 12 meses do exercício; liquidações 99,99% e pagamentos 99,7% até dezembro do ano seguinte.
MESES_DE_REMESSA: dict[str, int] = {
    NOTAS_EMPENHOS: 12,
    LIQUIDACOES: 24,
    NOTAS_PAGAMENTOS: 24,
    PAGAMENTOS_LIQUIDACOES: 24,
}
# A fonte só tem a ligação liquidação -> pagamento em remessas de 2025 em diante (vazia antes).
PRIMEIRA_REMESSA: dict[str, str] = {PAGAMENTOS_LIQUIDACOES: "202501"}
# Remessa do mês corrente e do anterior ainda pode chegar: nunca vem do cache (análogo à D-23).
MESES_DE_REMESSA_ABERTOS = 2


def _mes(ano: int, indice: int) -> str:
    """`AAAAMM` do mês `indice` (0 = janeiro de `ano`)."""
    return f"{ano + indice // 12}{indice % 12 + 1:02d}"


def mes_corrente(hoje: date) -> str:
    return f"{hoje.year}{hoje.month:02d}"


def meses_de_remessa(endpoint: str, exercicio: int, hoje: date) -> list[str]:
    """Meses de remessa coletados para o exercício: a janela do endpoint, nunca além do mês corrente."""
    teto = mes_corrente(hoje)
    piso = PRIMEIRA_REMESSA.get(endpoint, "")
    meses = (_mes(exercicio, i) for i in range(MESES_DE_REMESSA[endpoint]))
    return [m for m in meses if piso <= m <= teto]


def remessa_aberta(mes: str, hoje: date) -> bool:
    indice_hoje = hoje.year * 12 + hoje.month - 1
    indice_mes = int(mes[:4]) * 12 + int(mes[4:]) - 1
    return indice_mes > indice_hoje - MESES_DE_REMESSA_ABERTOS


def balde_execucao(exercicio: int, mes: str) -> str:
    """Nome do balde no layout do v2: `AAAA00_AAAAMM`."""
    return f"{exercicio}00_{mes}"


def formatar_data_janela(endpoint: str, ano: int, inicio: bool) -> str:
    """Primeiro ou último dia do ano, no formato que este endpoint aceita."""
    mes_dia = "01-01" if inicio else "12-31"
    if endpoint in ENDPOINTS_DATA_COM_HIFEN:
        return f"{ano}-{mes_dia}"
    return f"{ano}{mes_dia.replace('-', '')}"


# ---------------------------------------------------------------------------
# Rede: retry com backoff que conta 429 e 5xx.
# ---------------------------------------------------------------------------

# 429 não é erro observável: o Retry o absorve e o devolve como latência. Sem este contador,
# "a coleta terminou sem erro" não distingue coleta tranquila de coleta estrangulada.
_CONTADOR_REDE: Counter[str] = Counter()
_TRAVA_CONTADOR = threading.Lock()


class _RetryQueConta(Retry):
    """`Retry` que conta cada retentativa e o status que a motivou.

    `increment` é o único ponto por onde toda retentativa passa, e `Retry.new` preserva a
    subclasse (`type(self)`), então a contagem sobrevive à cadeia de retentativas.
    """

    def increment(self, *args: Any, response: Any = None, **kwargs: Any) -> Retry:
        status = getattr(response, "status", None)
        with _TRAVA_CONTADOR:
            _CONTADOR_REDE["retentativas"] += 1
            if status == 429:
                _CONTADOR_REDE["http429"] += 1
            elif status is not None and 500 <= status < 600:
                _CONTADOR_REDE["http5xx"] += 1
        return super().increment(*args, response=response, **kwargs)


def contador_de_rede() -> dict[str, int]:
    with _TRAVA_CONTADOR:
        return {"retentativas": 0, "http429": 0, "http5xx": 0, **_CONTADOR_REDE}


def zerar_contador_de_rede() -> None:
    with _TRAVA_CONTADOR:
        _CONTADOR_REDE.clear()


def criar_sessao(total_retentativas: int = 5, backoff: float = 1.5) -> requests.Session:
    """Sessão com retry em 429 e 5xx (respeita `Retry-After`), backoff exponencial."""
    sessao = requests.Session()
    retry = _RetryQueConta(
        total=total_retentativas,
        backoff_factor=backoff,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET"],
    )
    adaptador = HTTPAdapter(max_retries=retry)
    sessao.mount("https://", adaptador)
    sessao.mount("http://", adaptador)
    return sessao


def paginar(buscar_pagina: Callable[[int], list[dict]], tamanho_pagina: int) -> Iterator[list[dict]]:
    """Percorre as páginas por `$start_index`; termina em página curta ou vazia.

    Uma página com exatamente `tamanho_pagina` registros não é a última: busca a próxima.
    """
    inicio = 0
    while True:
        elementos = buscar_pagina(inicio)
        if not elementos:
            return
        yield elementos
        if len(elementos) < tamanho_pagina:
            return
        inicio += tamanho_pagina


# ---------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------


def caminho_cache(raiz: Path, endpoint: str, municipio: str, balde: str | int, gzip: bool = False) -> Path:
    return Path(raiz) / endpoint / str(municipio) / f"{balde}.json{'.gz' if gzip else ''}"


def salvar_cache(caminho: Path, registros: list[dict]) -> None:
    """Grava em `<arquivo>.tmp` e troca com `os.replace`: uma interrupção nunca deixa arquivo truncado.
    Caminho terminado em `.gz` é gravado com gzip (execução orçamentária: cerca de 23x menor)."""
    caminho.parent.mkdir(parents=True, exist_ok=True)
    temporario = caminho.with_name(caminho.name + ".tmp")
    abrir = gzip.open if caminho.suffix == ".gz" else open
    with abrir(temporario, "wt", encoding="utf-8") as arquivo:
        json.dump(registros, arquivo, ensure_ascii=False)
        arquivo.flush()
    with temporario.open("rb") as arquivo:
        os.fsync(arquivo.fileno())
    os.replace(temporario, caminho)


def _ler_lista(caminho: Path) -> tuple[list | None, str]:
    """`(lista, "valido")` ou `(None, motivo)`: `ausente`, `vazio` ou `truncado` (JSON ou gzip ilegível,
    ou que não é lista)."""
    if not caminho.is_file():
        return None, "ausente"
    try:
        if caminho.suffix == ".gz":
            with gzip.open(caminho, "rt", encoding="utf-8") as arquivo:
                texto = arquivo.read()
        else:
            texto = caminho.read_text(encoding="utf-8")
    except (OSError, EOFError, UnicodeDecodeError):
        return None, "truncado"
    if not texto.strip():
        return None, "vazio"
    try:
        registros = json.loads(texto)
    except json.JSONDecodeError:
        return None, "truncado"
    if not isinstance(registros, list):
        return None, "truncado"
    return registros, "valido"


def _registro_no_balde(registro: object, municipio: str, campo: str | None, ano: int | None) -> bool:
    if not isinstance(registro, dict) or str(registro.get("codigo_municipio", "")).strip() != municipio:
        return False
    if campo is None or ano is None:
        return True
    return str(registro.get(campo) or "").strip()[:4] == str(ano)


def ler_cache_valido(
    caminho: Path, municipio: str, endpoint: str, ano: int | None
) -> tuple[list[dict] | None, str]:
    """`(registros, "valido")` ou `(None, motivo)`.

    Motivos: `ausente`, `vazio` (0 bytes: gravação interrompida antes de começar), `truncado`
    (JSON ilegível ou que não é lista), `janela_divergente` (registro de outro município ou fora
    do ano). Uma lista vazia `[]` é válida: é o que a API devolve para balde sem registro.
    """
    registros, motivo = _ler_lista(caminho)
    if registros is None:
        return None, motivo
    campo = CAMPO_JANELA.get(endpoint)
    if not all(_registro_no_balde(r, municipio, campo, ano) for r in registros):
        return None, "janela_divergente"
    return registros, "valido"


def tem_registro_repetido(registros: list[dict]) -> bool:
    """Registro completo repetido no balde é o sintoma da paginação instável (um repetido ocupa o lugar
    de um registro perdido). Medido: 0 repetições em todo balde de página única do cache do v2."""
    vistos = {json.dumps(r, sort_keys=True, ensure_ascii=False) for r in registros}
    return len(vistos) != len(registros)


def _registro_na_remessa(registro: object, municipio: str, exercicio: int, mes: str) -> bool:
    return (
        isinstance(registro, dict)
        and str(registro.get("codigo_municipio", "")).strip() == municipio
        and str(registro.get("exercicio_orcamento", "")).strip() == f"{exercicio}00"
        and str(registro.get("data_referencia_doc", "")).strip() == mes
    )


def validar_execucao(registros: list, municipio: str, exercicio: int, mes: str) -> str:
    """`valido`, `janela_divergente` (registro de outro município, exercício ou mês de remessa) ou
    `registro_repetido`."""
    if not all(_registro_na_remessa(r, municipio, exercicio, mes) for r in registros):
        return "janela_divergente"
    if tem_registro_repetido(registros):
        return "registro_repetido"
    return "valido"


def ler_cache_execucao_valido(
    caminho: Path, municipio: str, exercicio: int, mes: str
) -> tuple[list[dict] | None, str]:
    """Como `ler_cache_valido`, para o balde da execução orçamentária (`.json.gz` ou `.json`).

    Nenhum registro completo repetido é condição SUFICIENTE de completude: a paginação devolve N
    linhas de um conjunto de N registros; N linhas distintas são, portanto, o conjunto inteiro (casa
    dos pombos). Premissas: a fonte não muda durante a coleta do balde e não tem duplicata legítima
    (0 repetições em todo balde de página única do v2 e nas coletas ordenadas da sondagem).
    """
    registros, motivo = _ler_lista(caminho)
    if registros is None:
        return None, motivo
    motivo = validar_execucao(registros, municipio, exercicio, mes)
    return (registros, motivo) if motivo == "valido" else (None, motivo)


def ler_rodadas_execucao_validas(
    rodadas: list[Path], municipio: str, exercicio: int, mes: str
) -> tuple[list[dict] | None, str]:
    """União das rodadas `_rN` da saturação do v2 (`/liquidacoes`), deduplicada por registro completo.

    Aceita só se toda rodada é íntegra e da janela, todas têm o mesmo número de linhas N (o universo
    não mudou entre elas) e a união tem exatamente N registros distintos: pelo mesmo argumento da
    casa dos pombos, é o balde completo. Senão: `rodadas_divergentes` ou `registro_repetido`
    (a união não cobriu o universo).
    """
    if not rodadas:
        return None, "ausente"
    uniao: dict[str, dict] = {}
    linhas: set[int] = set()
    for caminho in rodadas:
        registros, motivo = _ler_lista(caminho)
        if registros is None:
            return None, motivo
        if not all(_registro_na_remessa(r, municipio, exercicio, mes) for r in registros):
            return None, "janela_divergente"
        linhas.add(len(registros))
        for registro in registros:
            uniao.setdefault(json.dumps(registro, sort_keys=True, ensure_ascii=False), registro)
    if len(linhas) != 1:
        return None, "rodadas_divergentes"
    if len(uniao) != linhas.pop():
        return None, "registro_repetido"
    return list(uniao.values()), "valido"


def _rodadas(raiz: Path, endpoint: str, municipio: str, balde: str) -> list[Path]:
    pasta = Path(raiz) / endpoint / municipio
    numeradas = []
    for caminho in pasta.glob(f"{balde}_r*.json"):
        sufixo = caminho.stem.removeprefix(f"{balde}_r")
        if sufixo.isdigit():
            numeradas.append((int(sufixo), caminho))
    return [caminho for _, caminho in sorted(numeradas)]


# ---------------------------------------------------------------------------
# Relatório de origem dos baldes
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Balde:
    endpoint: str
    municipio: str
    balde: str
    origem: str  # cache | cache_extra | rede | faltando
    registros: int = 0
    motivo: str | None = None  # por que o cache não serviu, ou por que faltou


@dataclass
class RelatorioExtracao:
    baldes: list[Balde] = field(default_factory=list)

    def registrar(self, balde: Balde) -> None:
        self.baldes.append(balde)

    @property
    def faltando(self) -> list[Balde]:
        return [b for b in self.baldes if b.origem == "faltando"]

    def por_origem(self) -> dict[str, int]:
        return dict(Counter(b.origem for b in self.baldes))

    def linhas(self) -> list[str]:
        """Texto do relatório final: contagem por origem, rede e a lista do que faltou."""
        saida = [f"baldes por origem: {self.por_origem()}", f"rede: {contador_de_rede()}"]
        invalidos = Counter(b.motivo for b in self.baldes if b.origem == "rede" and b.motivo)
        if invalidos:
            saida.append(f"cache recusado e recoletado: {dict(invalidos)}")
        for b in self.faltando:
            saida.append(f"FALTANDO {b.endpoint}/{b.municipio}/{b.balde}: {b.motivo}")
        return saida


# ---------------------------------------------------------------------------
# Extrator
# ---------------------------------------------------------------------------


class Extrator:
    """Obtém baldes do cache (principal, depois extra) ou da API, e registra a origem de cada um."""

    def __init__(
        self,
        base_url: str,
        cache: Path,
        cache_extra: list[Path] | None = None,
        tamanho_pagina: int = 1000,
        intervalo: float = 0.3,
        timeout: tuple[float, float] = (10.0, 60.0),
        so_cache: bool = False,
        sessao: requests.Session | None = None,
        hoje: Callable[[], date] = date.today,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.cache = Path(cache)
        self.cache_extra = [Path(p) for p in cache_extra or []]
        self.tamanho_pagina = tamanho_pagina
        self.intervalo = intervalo
        self.timeout = timeout
        self.so_cache = so_cache
        self._sessao = sessao
        self._hoje = hoje
        self._ultima_chamada = 0.0
        self.relatorio = RelatorioExtracao()

    @property
    def sessao(self) -> requests.Session:
        if self._sessao is None:
            self._sessao = criar_sessao()
        return self._sessao

    def _esperar_intervalo(self) -> None:
        """Respeita um intervalo mínimo entre chamadas (rate limit configurável)."""
        espera = self._ultima_chamada + self.intervalo - time.monotonic()
        if espera > 0:
            time.sleep(espera)
        self._ultima_chamada = time.monotonic()

    def _buscar_pagina(self, endpoint: str, parametros: dict[str, Any], inicio: int) -> list[dict]:
        self._esperar_intervalo()
        resposta = self.sessao.get(
            f"{self.base_url}/{endpoint}",
            params={**parametros, "$format": "json", "$count": self.tamanho_pagina, "$start_index": inicio},
            timeout=self.timeout,
        )
        resposta.raise_for_status()
        corpo = resposta.json()
        elementos = corpo.get("elements") if isinstance(corpo, dict) else None
        if not isinstance(elementos, list):
            raise ValueError(f"resposta sem 'elements' em {endpoint} (start_index={inicio})")
        return elementos

    def coletar(self, endpoint: str, parametros: dict[str, Any]) -> list[dict]:
        """Todas as páginas de uma consulta à API."""
        registros: list[dict] = []
        for pagina in paginar(
            lambda inicio: self._buscar_pagina(endpoint, parametros, inicio), self.tamanho_pagina
        ):
            registros.extend(pagina)
        return registros

    def _parametros(self, endpoint: str, municipio: str, ano: int | None) -> dict[str, Any]:
        parametros: dict[str, Any] = {"codigo_municipio": municipio}
        if ano is not None:
            parametros["data_inicio"] = formatar_data_janela(endpoint, ano, inicio=True)
            parametros["data_fim"] = formatar_data_janela(endpoint, ano, inicio=False)
        return parametros

    def obter(self, endpoint: str, municipio: str, ano: int | None = None) -> list[dict] | None:
        """Registros de um balde, ou None se ele não pôde ser obtido (registrado como `faltando`)."""
        balde = str(ano) if ano is not None else BALDE_SEM_JANELA
        ano_aberto = ano is not None and ano >= self._hoje().year
        motivo: str | None = None

        if ano_aberto and not self.so_cache:
            motivo = "ano_corrente"
        else:
            for origem, raiz in [("cache", self.cache)] + [("cache_extra", r) for r in self.cache_extra]:
                registros, motivo_raiz = ler_cache_valido(
                    caminho_cache(raiz, endpoint, municipio, balde), municipio, endpoint, ano
                )
                if registros is not None:
                    self.relatorio.registrar(Balde(endpoint, municipio, balde, origem, len(registros)))
                    return registros
                if motivo_raiz != "ausente":
                    logger.warning(
                        "cache inválido em %s para %s/%s/%s: %s",
                        raiz,
                        endpoint,
                        municipio,
                        balde,
                        motivo_raiz,
                    )
                    motivo = motivo or motivo_raiz

        if self.so_cache:
            self.relatorio.registrar(
                Balde(endpoint, municipio, balde, "faltando", motivo=motivo or "ausente")
            )
            logger.error("balde ausente no cache: %s/%s/%s", endpoint, municipio, balde)
            return None
        try:
            registros = self.coletar(endpoint, self._parametros(endpoint, municipio, ano))
        except (requests.RequestException, ValueError) as erro:
            self.relatorio.registrar(Balde(endpoint, municipio, balde, "faltando", motivo=f"rede: {erro}"))
            logger.error("falha ao coletar %s/%s/%s: %s", endpoint, municipio, balde, erro)
            return None
        salvar_cache(caminho_cache(self.cache, endpoint, municipio, balde), registros)
        self.relatorio.registrar(Balde(endpoint, municipio, balde, "rede", len(registros), motivo))
        logger.info("%s/%s/%s: %d registros da API", endpoint, municipio, balde, len(registros))
        return registros

    def ler_execucao_do_cache(
        self, endpoint: str, municipio: str, exercicio: int, mes: str
    ) -> tuple[list[dict] | None, str, str | None]:
        """`(registros, origem, motivo)` do primeiro cache válido, ou `(None, "", motivo)`.

        Ordem (a regra única de reaproveitamento): o cache principal (`.json.gz`, o que este extrator
        grava) tem precedência; depois cada cache extra, no layout do v2: `.json.gz`, `.json` e, por
        último, a união das rodadas `_rN` (`ler_rodadas_execucao_validas`). Todo arquivo passa pela
        mesma validação; o motivo da primeira recusa vai para o relatório.
        """
        balde = balde_execucao(exercicio, mes)
        candidatos: list[tuple[str, Path | list[Path]]] = [
            ("cache", caminho_cache(self.cache, endpoint, municipio, balde, gzip=True))
        ]
        for raiz in self.cache_extra:
            candidatos += [
                ("cache_extra", caminho_cache(raiz, endpoint, municipio, balde, gzip=True)),
                ("cache_extra", caminho_cache(raiz, endpoint, municipio, balde)),
                ("cache_extra", _rodadas(raiz, endpoint, municipio, balde)),
            ]
        motivo: str | None = None
        for origem, onde in candidatos:
            if isinstance(onde, list):
                registros, motivo_aqui = ler_rodadas_execucao_validas(onde, municipio, exercicio, mes)
            else:
                registros, motivo_aqui = ler_cache_execucao_valido(onde, municipio, exercicio, mes)
            if registros is not None:
                return registros, origem, None
            if motivo_aqui != "ausente":
                logger.warning(
                    "cache recusado para %s/%s/%s: %s (%s)", endpoint, municipio, balde, motivo_aqui, onde
                )
                motivo = motivo or motivo_aqui
        return None, "", motivo

    def obter_execucao(self, endpoint: str, municipio: str, exercicio: int, mes: str) -> list[dict] | None:
        """Registros de um balde da execução orçamentária, ou None se faltou (registrado como `faltando`).

        Leitura e regra num lugar só: `sarc/pretericao` lê por aqui (em geral com `so_cache`). Sempre
        com `$orderby`, `exercicio_orcamento` e `data_referencia_doc` (sem o mês a API devolve outra
        projeção, com CPF em claro). Balde com registro repetido (cache ou rede) é recusado; se a
        recoleta ainda repetir, fica `faltando` com motivo `registro_repetido`: nunca subamostra.
        """
        balde = balde_execucao(exercicio, mes)
        motivo: str | None = None
        if remessa_aberta(mes, self._hoje()) and not self.so_cache:
            motivo = "remessa_aberta"
        else:
            registros, origem, motivo = self.ler_execucao_do_cache(endpoint, municipio, exercicio, mes)
            if registros is not None:
                self.relatorio.registrar(Balde(endpoint, municipio, balde, origem, len(registros)))
                return registros

        if self.so_cache:
            self.relatorio.registrar(
                Balde(endpoint, municipio, balde, "faltando", motivo=motivo or "ausente")
            )
            logger.error("balde ausente no cache: %s/%s/%s", endpoint, municipio, balde)
            return None
        parametros = {
            "codigo_municipio": municipio,
            "exercicio_orcamento": f"{exercicio}00",
            "data_referencia_doc": mes,
            "$orderby": ORDENACAO[endpoint],
        }
        validacao = "registro_repetido"
        try:
            for _ in range(2):
                registros = self.coletar(endpoint, parametros)
                validacao = validar_execucao(registros, municipio, exercicio, mes)
                if validacao != "registro_repetido":
                    break
                logger.warning(
                    "%s/%s/%s: registro repetido na coleta; coletando de novo", endpoint, municipio, balde
                )
        except (requests.RequestException, ValueError) as erro:
            self.relatorio.registrar(Balde(endpoint, municipio, balde, "faltando", motivo=f"rede: {erro}"))
            logger.error("falha ao coletar %s/%s/%s: %s", endpoint, municipio, balde, erro)
            return None
        if validacao != "valido":
            self.relatorio.registrar(Balde(endpoint, municipio, balde, "faltando", motivo=validacao))
            logger.error("balde recusado após a coleta %s/%s/%s: %s", endpoint, municipio, balde, validacao)
            return None
        salvar_cache(caminho_cache(self.cache, endpoint, municipio, balde, gzip=True), registros)
        self.relatorio.registrar(Balde(endpoint, municipio, balde, "rede", len(registros), motivo))
        logger.info("%s/%s/%s: %d registros da API", endpoint, municipio, balde, len(registros))
        return registros

    def listar_municipios(self) -> list[dict]:
        """Todos os municípios da API (`/municipios` sem código). Não usa cache: é uma chamada só."""
        return self.coletar(MUNICIPIOS, {})
