"""Cálculo da preterição de um município: obrigações, junção com o pagamento, filas e componentes por credor.

Funções puras sobre registros já lidos do cache (`sarc.pretericao.leitor`); nada aqui toca o grafo nem o
disco. Definições (docs/propostas/pretericao.md §1, §3, §6; DECISOES D-36):

- **chave do empenho** = (município, exercício, órgão, número do empenho). O número é sequência local do
  órgão dentro do exercício: sem o órgão, de 1,3% a 8,2% dos empenhos ficam com credor conflitante;
  sem o exercício, a liquidação herda o credor de outro ano (v2, regra 2). Se ainda assim dois registros
  da mesma chave divergem em credor, fonte ou natureza, as liquidações dele são excluídas e contadas
  (`empenho_conflitante`), nunca atribuídas ao primeiro da ordem;
- **obrigação** = liquidação não estornada cujo empenho está numa das 4 categorias do art. 141;
  posição na fila = data da liquidação (exigibilidade);
- **fila** = (município, órgão, fonte de recursos, categoria);
- **pagamento da obrigação**: pela tabela de ligação (`/pagamentos_liquidacoes`, que só existe em remessas
  de 2025 em diante), casada pela DATA da liquidação e nunca pelo sub-empenho, que é numerado de outro
  jeito em cada endpoint (o v2 casava pelo sub e 34,1% das obrigações pagas recebiam pagamento anterior
  à liquidação); sem ligação, a heurística H: dentro do empenho, o primeiro pagamento ainda livre com
  data >= a da liquidação, preferindo o de mesmo valor. A origem fica em cada obrigação;
- **corte** = a maior data de pagamento observada no município: a obrigação não paga espera até ele;
- **ultrapassagens(j)** = obrigações da mesma fila liquidadas DEPOIS de j e pagas ANTES de j (ou antes do
  corte, se j não foi paga). É a leitura do credor **preterido**, o que os acórdãos condenam;
- dois componentes por credor, **nunca somados**: principal (obrigações pagas: só ele ordena o ranking)
  e auxiliar (obrigações não pagas até o corte, que misturam inadimplência, falha da junção e resto a
  pagar cancelado);
- a leitura do v2 (quem foi pago enquanto havia obrigação anterior pendente) fica como coluna:
  `inversao(p)` e `atraso(p)`.
"""

from __future__ import annotations

import bisect
from collections import Counter, defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import date

from sarc.pretericao.categoria import CATEGORIAS_DA_FILA, FORA_DO_ESCOPO, INDEFINIDO, classificar

LIGACAO = "ligacao"
HEURISTICA = "heuristica"
SEM_PAGAMENTO = "sem_pagamento"

TOLERANCIA_VALOR = 0.005
MAXIMO_EXEMPLOS = 5

Chave = tuple[str, str, str, str]  # (município, exercício AAAA00, órgão, número do empenho)


# ---------------------------------------------------------------------------
# Registros projetados (só os campos usados; o leitor descarta o resto).
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Empenho:
    chave: Chave
    elemento_despesa: str
    subelemento: str
    tipo_fonte: str
    codigo_fonte: str
    credor: str
    tipo_credor: str
    data_emissao: str  # AAAA-MM-DD ou ""


@dataclass(frozen=True, slots=True)
class Liquidacao:
    chave: Chave
    data: str
    valor: float
    estornada: bool


@dataclass(frozen=True, slots=True)
class Pagamento:
    chave: Chave
    numero: str
    data: str
    valor: float
    estornado: bool


@dataclass(frozen=True, slots=True)
class Ligacao:
    chave: Chave
    numero_pagamento: str
    data_liquidacao: str


@dataclass(slots=True)
class Obrigacao:
    chave: Chave
    fila: tuple[str, str, str, str, str]  # (município, órgão, tipo_fonte, código_fonte, categoria)
    credor: str  # CNPJ do credor PJ; "" para credor que não é PJ (nunca gravado)
    data_empenho: str
    data_liquidacao: str
    data_pagamento: str | None
    origem: str
    ultrapassagens: int = 0
    inversao: int = 0
    atraso: int = 0


@dataclass
class Montagem:
    obrigacoes: list[Obrigacao]
    total_liquidacoes: int
    exclusoes: Counter[str]
    origem: Counter[str]
    empenhos_conflitantes: int
    corte: str | None

    @property
    def reconciliacao_bate(self) -> bool:
        return sum(self.exclusoes.values()) + len(self.obrigacoes) == self.total_liquidacoes


def eh_credor_pj(empenho: Empenho) -> bool:
    """Credor pessoa jurídica: tipo 1 e CNPJ de 14 dígitos. PF e outros vêm cifrados (24 caracteres)."""
    return empenho.tipo_credor == "1" and len(empenho.credor) == 14 and empenho.credor.isdigit()


def _valor_igual(a: float, b: float) -> bool:
    return abs(a - b) < TOLERANCIA_VALOR


def montar_obrigacoes(
    empenhos: Iterable[Empenho],
    liquidacoes: Iterable[Liquidacao],
    pagamentos: Iterable[Pagamento],
    ligacoes: Iterable[Ligacao],
    heuristica: bool = True,
) -> Montagem:
    """Obrigações na fila, com o pagamento resolvido. Toda liquidação lida termina numa obrigação ou numa
    exclusão com motivo (reconciliação). O resultado não depende da ordem dos registros de entrada.
    `heuristica=False` só serve para medir a heurística: sem ligação, a obrigação fica sem pagamento."""
    por_chave: dict[Chave, Empenho] = {}
    conflitantes: set[Chave] = set()
    for e in sorted(empenhos, key=lambda e: (e.chave, e.data_emissao, e.credor, e.elemento_despesa)):
        anterior = por_chave.get(e.chave)
        if anterior is None:
            por_chave[e.chave] = e
        elif (
            anterior.credor,
            anterior.elemento_despesa,
            anterior.subelemento,
            anterior.tipo_fonte,
            anterior.codigo_fonte,
        ) != (e.credor, e.elemento_despesa, e.subelemento, e.tipo_fonte, e.codigo_fonte):
            conflitantes.add(e.chave)

    validos = sorted(
        (p for p in pagamentos if not p.estornado and p.data),
        key=lambda p: (p.chave, p.numero, p.data, p.valor),
    )
    pelo_numero: dict[tuple[Chave, str], Pagamento] = {}
    for p in validos:
        pelo_numero.setdefault((p.chave, p.numero), p)
    corte = max((p.data for p in validos), default=None)

    ligados: dict[tuple[Chave, str], list[Pagamento]] = defaultdict(list)
    usados: set[int] = set()
    for lig in sorted(ligacoes, key=lambda x: (x.chave, x.numero_pagamento, x.data_liquidacao)):
        p = pelo_numero.get((lig.chave, lig.numero_pagamento))
        if p is not None and lig.data_liquidacao and id(p) not in usados:
            ligados[(lig.chave, lig.data_liquidacao)].append(p)
            usados.add(id(p))
    for lista in ligados.values():
        lista.sort(key=lambda p: (p.data, p.numero))
    livres: dict[Chave, list[Pagamento]] = defaultdict(list)
    for p in sorted(validos, key=lambda p: (p.data, p.numero)):
        if id(p) not in usados:
            livres[p.chave].append(p)

    exclusoes: Counter[str] = Counter()
    candidatas: list[tuple[Liquidacao, Empenho, str]] = []
    total = 0
    for liq in liquidacoes:
        total += 1
        if liq.estornada:
            exclusoes["liquidacao_estornada"] += 1
            continue
        empenho = por_chave.get(liq.chave)
        if empenho is None:
            exclusoes["sem_empenho"] += 1
            continue
        if liq.chave in conflitantes:  # falha fechada: credor, fonte ou categoria ambíguos
            exclusoes["empenho_conflitante"] += 1
            continue
        _, categoria = classificar(empenho.elemento_despesa, empenho.subelemento)
        if categoria == FORA_DO_ESCOPO:
            exclusoes["fora_do_escopo"] += 1
            continue
        if categoria == INDEFINIDO or categoria not in CATEGORIAS_DA_FILA:
            exclusoes["categoria_indefinida"] += 1
            continue
        if not _data_valida(liq.data):
            exclusoes["sem_data_de_liquidacao"] += 1
            continue
        candidatas.append((liq, empenho, categoria))

    # Ordem determinística: por data; na mesma data, o maior valor primeiro (casa o pagamento de mesmo valor).
    candidatas.sort(key=lambda t: (t[0].data, t[0].chave, -t[0].valor))
    restantes_na_data = Counter((liq.chave, liq.data) for liq, _, _ in candidatas)
    origem: Counter[str] = Counter()
    obrigacoes: list[Obrigacao] = []
    for liq, empenho, categoria in candidatas:
        data_pagamento, de_onde = _resolver_pagamento(
            liq, ligados, livres if heuristica else {}, restantes_na_data
        )
        origem[de_onde] += 1
        obrigacoes.append(
            Obrigacao(
                chave=liq.chave,
                fila=(liq.chave[0], liq.chave[2], empenho.tipo_fonte, empenho.codigo_fonte, categoria),
                credor=empenho.credor if eh_credor_pj(empenho) else "",
                data_empenho=empenho.data_emissao,
                data_liquidacao=liq.data,
                data_pagamento=data_pagamento,
                origem=de_onde,
            )
        )
    return Montagem(obrigacoes, total, exclusoes, origem, len(conflitantes), corte)


def _resolver_pagamento(
    liq: Liquidacao,
    ligados: dict[tuple[Chave, str], list[Pagamento]],
    livres: dict[Chave, list[Pagamento]],
    restantes_na_data: Counter[tuple[Chave, str]],
) -> tuple[str | None, str]:
    chave_data = (liq.chave, liq.data)
    lista = ligados.get(chave_data)
    if lista is not None:
        restantes_na_data[chave_data] -= 1
        if restantes_na_data[chave_data] <= 0:  # a última liquidação da data leva o que sobrou
            tomados = list(lista)
            lista.clear()
        else:  # várias liquidações na mesma data: cada uma leva o pagamento ligado de mesmo valor
            igual = [p for p in lista if _valor_igual(p.valor, liq.valor)]
            tomados = igual[:1] or lista[:1]
            for p in tomados:
                lista.remove(p)
        if tomados:
            return min(p.data for p in tomados), LIGACAO
        return None, SEM_PAGAMENTO
    candidatos = [p for p in livres.get(liq.chave, ()) if p.data >= liq.data]
    if not candidatos:
        return None, SEM_PAGAMENTO
    igual = [p for p in candidatos if _valor_igual(p.valor, liq.valor)]
    escolhido = (igual or candidatos)[0]
    livres[liq.chave].remove(escolhido)
    return escolhido.data, HEURISTICA


def _data_valida(texto: str) -> bool:
    try:
        date.fromisoformat(texto)
    except ValueError:
        return False
    return True


# ---------------------------------------------------------------------------
# Ultrapassagens (preterido) e inversão (v2, beneficiário), por fila, em O(n log n).
# ---------------------------------------------------------------------------


class _Fenwick:
    """Contagem por prefixo sobre índices 1..n."""

    def __init__(self, n: int) -> None:
        self.arvore = [0] * (n + 1)

    def somar(self, i: int) -> None:
        while i < len(self.arvore):
            self.arvore[i] += 1
            i += i & -i

    def prefixo(self, i: int) -> int:
        total = 0
        while i > 0:
            total += self.arvore[i]
            i -= i & -i
        return total


class _FenwickMinimo:
    """Mínimo por prefixo sobre índices 1..n, com atualização que só diminui."""

    def __init__(self, n: int) -> None:
        self.arvore = [10**9] * (n + 1)

    def baixar(self, i: int, valor: int) -> None:
        while i < len(self.arvore):
            if valor < self.arvore[i]:
                self.arvore[i] = valor
            i += i & -i

    def prefixo(self, i: int) -> int:
        menor = 10**9
        while i > 0:
            menor = min(menor, self.arvore[i])
            i -= i & -i
        return menor


def _ordinal(texto: str) -> int:
    return date.fromisoformat(texto).toordinal()


def calcular_fila(obrigacoes: Sequence[Obrigacao], corte: str) -> None:
    """Preenche `ultrapassagens`, `inversao` e `atraso` das obrigações de UMA fila (in-place)."""
    if not obrigacoes:
        return
    corte_o = _ordinal(corte)
    liq = [_ordinal(o.data_liquidacao) for o in obrigacoes]
    pag = [_ordinal(o.data_pagamento) if o.data_pagamento else None for o in obrigacoes]

    # Preterido: em ordem decrescente de liquidação, consulta o grupo (mesma data) antes de inserir.
    valores = sorted({p for p in pag if p is not None})
    arvore = _Fenwick(len(valores))
    ordem = sorted(range(len(obrigacoes)), key=lambda i: -liq[i])
    i = 0
    while i < len(ordem):
        j = i
        while j < len(ordem) and liq[ordem[j]] == liq[ordem[i]]:
            j += 1
        grupo = ordem[i:j]
        for k in grupo:
            limite = pag[k] if pag[k] is not None else corte_o
            obrigacoes[k].ultrapassagens = arvore.prefixo(bisect.bisect_left(valores, limite))
        for k in grupo:
            if pag[k] is not None:
                arvore.somar(bisect.bisect_left(valores, pag[k]) + 1)
        i = j

    # Beneficiário (leitura do v2): para o pagamento p, as obrigações liquidadas ANTES de p e ainda não
    # pagas quando p foi pago. Não paga conta como paga "no infinito". Índice invertido: maior data = 1.
    infinito = 10**9
    chaves = sorted({p if p is not None else infinito for p in pag}, reverse=True)
    posicao = {v: n + 1 for n, v in enumerate(chaves)}
    contagem = _Fenwick(len(chaves))
    mais_antiga = _FenwickMinimo(len(chaves))
    ordem = sorted(range(len(obrigacoes)), key=lambda i: liq[i])
    i = 0
    while i < len(ordem):
        j = i
        while j < len(ordem) and liq[ordem[j]] == liq[ordem[i]]:
            j += 1
        grupo = ordem[i:j]
        for k in grupo:
            if pag[k] is None:
                continue
            # pagas depois de p (data > pag[k]) = posições invertidas 1..(posição de pag[k]) - 1
            limite = posicao[pag[k]] - 1
            obrigacoes[k].inversao = contagem.prefixo(limite)
            obrigacoes[k].atraso = liq[k] - mais_antiga.prefixo(limite) if obrigacoes[k].inversao else 0
        for k in grupo:
            p = posicao[pag[k] if pag[k] is not None else infinito]
            contagem.somar(p)
            mais_antiga.baixar(p, liq[k])
        i = j


def calcular(montagem: Montagem) -> dict[tuple[str, str, str, str, str], list[Obrigacao]]:
    """Agrupa as obrigações por fila e calcula cada fila. Sem pagamento nenhum, não há corte nem cálculo."""
    filas: dict[tuple[str, str, str, str, str], list[Obrigacao]] = defaultdict(list)
    for o in montagem.obrigacoes:
        filas[o.fila].append(o)
    if montagem.corte is not None:
        for obrigacoes in filas.values():
            calcular_fila(obrigacoes, montagem.corte)
    return dict(filas)


# ---------------------------------------------------------------------------
# Agregados que vão para o grafo.
# ---------------------------------------------------------------------------


# Nível principal (D-49): obrigação ultrapassada que esperou mais que isto desde a liquidação, paga com esse
# atraso ou ainda não paga até o corte. Conservador ante as esperas dos acórdãos-base (628 a 2.144 dias).
ESPERA_MINIMA_DIAS = 180


@dataclass
class ResultadoCredor:
    """Um credor PJ numa fila. Os dois componentes do preterido nunca são somados."""

    credor: str
    n_obrigacoes: int = 0
    n_pagas: int = 0
    n_pagas_ultrapassadas: int = 0
    ultrapassagens_pagas: int = 0  # componente principal: ordena o ranking
    n_nao_pagas: int = 0
    ultrapassagens_nao_pagas: int = 0  # componente auxiliar
    maior_espera_nao_paga_dias: int = 0
    n_esperas_longas: int = 0  # nível principal (D-49): ultrapassadas com espera > ESPERA_MINIMA_DIAS
    soma_esperas_longas_dias: int = 0  # ordena o ranking principal
    n_pagamentos_fora_de_ordem: int = 0  # leitura do v2 (beneficiário), só coluna
    soma_atraso_beneficiario: int = 0
    origem: Counter[str] = field(default_factory=Counter)
    exemplos: list[str] = field(default_factory=list)


@dataclass
class ResultadoFila:
    fila: tuple[str, str, str, str, str]
    n_obrigacoes: int
    n_pagas: int
    n_credores_pj: int
    n_obrigacoes_credor_nao_pj: int
    n_ultrapassadas: int
    n_pagamentos_fora_de_ordem: int
    origem: Counter[str]
    credores: list[ResultadoCredor]


def agregar(
    filas: dict[tuple[str, str, str, str, str], list[Obrigacao]], corte: str | None
) -> list[ResultadoFila]:
    corte_o = _ordinal(corte) if corte else None
    resultado: list[ResultadoFila] = []
    for fila, obrigacoes in sorted(filas.items()):
        por_credor: dict[str, ResultadoCredor] = {}
        nao_pj = 0
        for o in obrigacoes:
            if not o.credor:
                nao_pj += 1
                continue
            r = por_credor.setdefault(o.credor, ResultadoCredor(o.credor))
            r.n_obrigacoes += 1
            r.origem[o.origem] += 1
            fim = o.data_pagamento or corte
            if o.ultrapassagens and fim:
                espera = _ordinal(fim) - _ordinal(o.data_liquidacao)
                if espera > ESPERA_MINIMA_DIAS:
                    r.n_esperas_longas += 1
                    r.soma_esperas_longas_dias += espera
            if o.data_pagamento is not None:
                r.n_pagas += 1
                r.ultrapassagens_pagas += o.ultrapassagens
                r.n_pagas_ultrapassadas += o.ultrapassagens > 0
                if o.inversao:
                    r.n_pagamentos_fora_de_ordem += 1
                    r.soma_atraso_beneficiario += o.atraso
            else:
                r.n_nao_pagas += 1
                r.ultrapassagens_nao_pagas += o.ultrapassagens
                if corte_o is not None:
                    espera = corte_o - _ordinal(o.data_liquidacao)
                    r.maior_espera_nao_paga_dias = max(r.maior_espera_nao_paga_dias, espera)
        for credor, r in por_credor.items():
            dele = sorted(
                (o for o in obrigacoes if o.credor == credor and o.ultrapassagens),
                key=lambda o: (-o.ultrapassagens, o.data_liquidacao, o.chave),
            )
            r.exemplos = [
                "|".join(
                    (
                        o.chave[3],
                        o.data_empenho,
                        o.data_liquidacao,
                        o.data_pagamento or "",
                        str(o.ultrapassagens),
                    )
                )
                for o in dele[:MAXIMO_EXEMPLOS]
            ]
        resultado.append(
            ResultadoFila(
                fila=fila,
                n_obrigacoes=len(obrigacoes),
                n_pagas=sum(1 for o in obrigacoes if o.data_pagamento is not None),
                n_credores_pj=len(por_credor),
                n_obrigacoes_credor_nao_pj=nao_pj,
                n_ultrapassadas=sum(1 for o in obrigacoes if o.ultrapassagens),
                n_pagamentos_fora_de_ordem=sum(1 for o in obrigacoes if o.inversao),
                origem=Counter(o.origem for o in obrigacoes),
                credores=sorted(por_credor.values(), key=lambda r: r.credor),
            )
        )
    return resultado


# ---------------------------------------------------------------------------
# Validação da heurística contra a ligação oficial (condição do revisor, D-36).
# ---------------------------------------------------------------------------

ACERTO = "acerto"
DATA_DIFERENTE = "data_diferente"
FALSO_NAO_PAGO = "falso_nao_pago"  # H não acha pagamento que a ligação tem: infla o componente auxiliar
FALSO_PAGO = "falso_pago"  # H dá pagamento que a ligação não tem
AMBOS_NAO_PAGO = "ambos_nao_pago"
SEM_VERDADE = (
    "sem_verdade"  # H paga depois de `ate`: a ligação pode faltar na fonte (cobertura irregular em 2026)
)


def comparar_heuristica(
    empenhos: Sequence[Empenho],
    liquidacoes: Sequence[Liquidacao],
    pagamentos: Sequence[Pagamento],
    ligacoes: Sequence[Ligacao],
    desde: str = "2025-01-01",
    ate: str = "2025-12-31",
) -> Counter[str]:
    """Compara, obrigação a obrigação, a heurística sozinha (sem a tabela de ligação) com a ligação oficial
    sozinha (sem heurística: sem ligação = não paga), nas obrigações liquidadas entre `desde` e `ate`.

    A ligação só existe em remessas de 2025 em diante, e mesmo nelas a cobertura varia: em 2025, 94% a 99%
    dos pagamentos estão ligados; em 2026, de 2% a 100% conforme o município (medido em 100, 104, 087, 030).
    Por isso "falso pago" (H paga, a ligação não) só conta se o pagamento de H é até `ate`; depois disso a
    obrigação vai para `sem_verdade`. "Falso não pago" (H não acha o pagamento que a ligação tem) é o erro
    que infla o componente auxiliar e vale em qualquer data."""
    oficial = montar_obrigacoes(empenhos, liquidacoes, pagamentos, ligacoes, heuristica=False)
    palpite = montar_obrigacoes(empenhos, liquidacoes, pagamentos, [])
    resultado: Counter[str] = Counter()
    # as duas montagens ordenam as obrigações do mesmo jeito (data, chave, valor): o par é posicional
    for verdade, h in zip(oficial.obrigacoes, palpite.obrigacoes, strict=True):
        if not desde <= verdade.data_liquidacao <= ate:
            continue
        if verdade.data_pagamento == h.data_pagamento:
            resultado[ACERTO if h.data_pagamento else AMBOS_NAO_PAGO] += 1
        elif h.data_pagamento is None:
            resultado[FALSO_NAO_PAGO] += 1
        elif verdade.data_pagamento is None:
            resultado[FALSO_PAGO if h.data_pagamento <= ate else SEM_VERDADE] += 1
        else:
            resultado[DATA_DIFERENTE] += 1
    return resultado
