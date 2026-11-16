"""Vínculo aproximado por contato declarado no certame (tabela 505): telefone cortado e endereço com
erro de digitação.

O contato declarado só liga um par no nível principal quando as duas empresas escreveram a mesma chave
(`sarc.grafo.chaves`). A fonte, porém, corta o telefone em 11 caracteres ('(81)3093331' em vez de
'(81)3093-3317': 5,4% dos registros) e o endereço é texto livre, com CEP genérico de um lado e específico
do outro, letra trocada ou abreviação. O vínculo se perde sem erro nenhum. Este módulo compara as formas
declaradas e devolve o padrão que as liga, com o grau da família (quantas empresas usam um contato
compatível), para a etapa `sarc vinculo-aproximado` gravar como aresta `VINCULO_APROXIMADO`.

Regras calibradas em 12/11/2026 contra um modelo nulo (pares sorteados de certames diferentes do mesmo
município e ano), controles positivos e conferência manual de 30 pares (`docs/DECISOES.md`, D-47):

- nível **aproximado** (abaixo do auxiliar, rótulo próprio, nunca somado ao principal):
  - `telefone_aproximado`: o mesmo número com outro formato. Padrões `nono` (com e sem o nono dígito),
    `prefixo` (um é o começo do outro, com DDD e ao menos 7 dígitos), `sem_ddd` e `igual_parcial` (os
    dois cortados do mesmo jeito). Grau da família até 4. Precisão por par 0,88 (IC 95% 0,83 a 0,92);
  - `endereco_aproximado`: o mesmo número da porta e o nome da rua quase igual (Jaro-Winkler ou
    Monge-Elkan >= 0,93 com o mesmo CEP de 5 dígitos, ou >= 0,97 só com o mesmo município), complemento
    que não conflita, grau abaixo de 5. Precisão de 0,80 a 0,94, conforme o modelo nulo;
- nível **pista** (abaixo do aproximado; aparece na ocorrência, não entra em contagem de alerta):
  - `telefone_digito`: um dígito diferente no meio do número (só as duas empresas usam) ou um dígito a
    mais ou a menos. Precisão 0,77 (0,64 a 0,84);
  - `mesmo_predio`: endereço aproximado com sala, loja ou apartamento diferentes.

Descartados por não ter sinal acima do acaso: dígitos vizinhos trocados, só o último dígito diferente
(linhas vizinhas do mesmo PABX), qualquer padrão com grau 5 ou mais, fragmento '(DD)NNNN-NN' (DDD + 5 ou
6 dígitos) e endereço sem número.
"""

from __future__ import annotations

import re
import unicodedata
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass

APROXIMADO = "aproximado"
PISTA = "pista"

TELEFONE_APROXIMADO = "telefone_aproximado"
TELEFONE_DIGITO = "telefone_digito"
ENDERECO_APROXIMADO = "endereco_aproximado"
MESMO_PREDIO = "mesmo_predio"

GRAU_MAXIMO = 5  # o mesmo limiar do contato exato (GRAU_MAXIMO_CONTATO_PADRAO)

# ------------------------------------------------------------------------------------------------
# Telefone
# ------------------------------------------------------------------------------------------------
DDD_VALIDOS = frozenset(
    int(d)
    for d in """
    11 12 13 14 15 16 17 18 19 21 22 24 27 28 31 32 33 34 35 37 38 41 42 43 44 45 46 47 48 49 51 53 54 55
    61 62 63 64 65 66 67 68 69 71 73 74 75 77 79 81 82 83 84 85 86 87 88 89 91 92 93 94 95 96 97 98 99
    """.split()
)
_DDD_ENTRE_PARENTESES = re.compile(r"^\s*\(\s*0?\d{2}\s*\)")
_MIN_PREFIXO = 9  # DDD + 7 dígitos

# Padrões de telefone, do mais forte para o mais fraco, e o nível de cada um.
NONO = "nono"
IGUAL_PARCIAL = "igual_parcial"
PREFIXO = "prefixo"
SEM_DDD = "sem_ddd"
SUBST1 = "subst1"
INDEL1 = "indel1"
_TRANSP = "transp"
_SUBST1_ULT = "subst1_ult"
PADROES_DE_FORMATO = (NONO, IGUAL_PARCIAL, PREFIXO, SEM_DDD)
PADROES_DE_DIGITO = (SUBST1, INDEL1)


def forma_telefone(bruto: object) -> str | None:
    """Telefone declarado reduzido a dígitos (8 a 11), sem zero de tronco e sem 55, ou None.

    Diferente de `chave_telefone`, aceita o número cortado ('(81)3093331' -> '813093331') e o número sem
    DDD. Recusa dígito repetido e o fragmento '(DD)NNNN-NN': com DDD + 6 dígitos, ele seria lido como
    número local de 8 dígitos e casaria com números que não o completam (conferência manual, par T06).
    """
    texto = "" if bruto is None else str(bruto).strip()
    digitos = re.sub(r"\D", "", texto).lstrip("0")
    if digitos.startswith("55") and len(digitos) in (12, 13):
        digitos = digitos[2:]
    if not 8 <= len(digitos) <= 11:
        return None
    if len(set(digitos[2:])) <= 1 or (len(set(digitos)) <= 2 and len(digitos) < 10):
        return None
    if _DDD_ENTRE_PARENTESES.match(texto) and len(digitos) <= 8:
        return None
    return digitos


def _completo(s: str) -> bool:
    return len(s) in (10, 11) and int(s[:2]) in DDD_VALIDOS


def _ddd_valido(s: str) -> bool:
    return int(s[:2]) in DDD_VALIDOS


def _sem_nono(s: str) -> str | None:
    """'85988887777' -> '8588887777' (celular sem o nono dígito)."""
    return s[:2] + s[3:] if len(s) == 11 and s[2] == "9" else None


def padrao_telefone(a: str, b: str) -> str | None:
    """Padrão mais forte que liga duas formas (`forma_telefone`), ou None. Formas iguais e completas são
    o vínculo exato, que já está no grafo: devolvem None."""
    if a == b:
        return None if _completo(a) else IGUAL_PARCIAL
    if len(a) > len(b):
        a, b = b, a
    la, lb = len(a), len(b)
    if la == 10 and lb == 11 and _completo(b) and _sem_nono(b) == a and a[2] in "6789":
        return NONO
    if la >= _MIN_PREFIXO and _completo(b) and _ddd_valido(a):
        if b.startswith(a):
            return PREFIXO
        sem_nono = _sem_nono(b)
        if sem_nono and sem_nono.startswith(a) and a[2] in "6789":
            return PREFIXO
    if la in (8, 9) and _completo(b) and (b[2:] == a or (la == 8 and _sem_nono(b) == b[:2] + a)):
        return SEM_DDD
    if la == lb and _completo(a) and _completo(b) and a[:2] == b[:2]:
        diferentes = [k for k in range(la) if a[k] != b[k]]
        if len(diferentes) == 2:
            i, j = diferentes
            if j == i + 1 and a[i] == b[j] and a[j] == b[i]:
                return _TRANSP
        if len(diferentes) == 1:
            return _SUBST1_ULT if diferentes[0] == la - 1 else SUBST1
    if lb == la + 1 and _completo(a) and _completo(b) and a[:2] == b[:2]:
        if any(b[:k] + b[k + 1 :] == a for k in range(lb)):
            return INDEL1
    return None


class FamiliaTelefone:
    """Grau da família de uma forma: quantas raízes de CNPJ usam um número compatível com ela (igual,
    que a estende, com a mesma parte local, com ou sem o nono dígito). É o análogo do `grauNacional` para
    o número cortado. Alimente com toda a 505 e com os telefones da Receita das licitantes."""

    def __init__(self) -> None:
        self._indice: dict[tuple[str, str], set[str]] = defaultdict(set)

    def adicionar(self, forma: str, raiz: str) -> None:
        indice = self._indice
        indice[("=", forma)].add(raiz)
        variantes = [forma]
        sem_nono = _sem_nono(forma)
        if sem_nono:
            variantes.append(sem_nono)
        for v in variantes:
            for k in range(_MIN_PREFIXO, len(v)):
                indice[("P", v[:k])].add(raiz)
        if _completo(forma):
            indice[("L", forma[2:])].add(raiz)
            if sem_nono:
                indice[("L", sem_nono[2:])].add(raiz)

    def raizes(self, forma: str) -> set[str]:
        indice = self._indice
        chaves = [("=", forma), ("L", forma)]
        if len(forma) >= _MIN_PREFIXO:
            chaves.append(("P", forma))
        if _completo(forma):
            chaves.append(("=", forma[2:]))  # o mesmo número declarado sem DDD
            sem_nono = _sem_nono(forma)
            if sem_nono:
                chaves += [("=", sem_nono), ("=", sem_nono[2:])]
            elif len(forma) == 10 and forma[2] in "6789":
                chaves.append(("=", forma[:2] + "9" + forma[2:]))
        achadas: set[str] = set()
        for chave in chaves:
            achadas |= indice.get(chave, set())
        return achadas

    def grau(self, *formas: str) -> int:
        return max((len(self.raizes(f)) for f in formas), default=0)


@dataclass(frozen=True)
class VinculoAproximado:
    tipo: str  # telefone_aproximado, telefone_digito, endereco_aproximado, mesmo_predio
    padrao: str
    nivel: str  # aproximado | pista
    grau: int


_FORCA_TELEFONE = {p: i for i, p in enumerate((NONO, IGUAL_PARCIAL, PREFIXO, SEM_DDD, SUBST1, INDEL1))}


def comparar_telefones(
    formas_a: Iterable[str], formas_b: Iterable[str], familia: FamiliaTelefone, grau_nacional: dict[str, int]
) -> VinculoAproximado | None:
    """O vínculo aproximado mais forte entre os telefones que as duas empresas declararam no certame.

    Se algum par de formas já é o mesmo número completo, o vínculo é o exato e não há aproximado. O grau
    é o da família, ou o `grauNacional` do número completo, o que for maior (falha fechada)."""
    formas_a, formas_b = set(formas_a), set(formas_b)
    if any(_completo(f) for f in formas_a & formas_b):
        return None
    melhor: VinculoAproximado | None = None
    for fa in formas_a:
        for fb in formas_b:
            padrao = padrao_telefone(fa, fb)
            if padrao not in _FORCA_TELEFONE:
                continue
            grau = max(familia.grau(fa, fb), grau_nacional.get(fa, 0), grau_nacional.get(fb, 0))
            if padrao in PADROES_DE_FORMATO:
                if grau >= GRAU_MAXIMO:
                    continue
                candidato = VinculoAproximado(TELEFONE_APROXIMADO, padrao, APROXIMADO, grau)
            elif padrao == SUBST1:
                if grau > 2:
                    continue
                candidato = VinculoAproximado(TELEFONE_DIGITO, padrao, PISTA, grau)
            else:
                if grau >= GRAU_MAXIMO:
                    continue
                candidato = VinculoAproximado(TELEFONE_DIGITO, padrao, PISTA, grau)
            if melhor is None or _FORCA_TELEFONE[candidato.padrao] < _FORCA_TELEFONE[melhor.padrao]:
                melhor = candidato
    return melhor


# ------------------------------------------------------------------------------------------------
# Endereço
# ------------------------------------------------------------------------------------------------
_TIPOS = {
    "R": "RUA", "RUA": "RUA", "RU": "RUA", "AV": "AV", "AVENIDA": "AV", "AVN": "AV", "AVE": "AV",
    "TV": "TV", "TRAV": "TV", "TRAVESSA": "TV", "TR": "TV", "ROD": "ROD", "RODOVIA": "ROD",
    "AL": "AL", "ALAMEDA": "AL", "PC": "PCA", "PCA": "PCA", "PRACA": "PCA", "PR": "PCA",
    "EST": "EST", "ESTRADA": "EST", "ESTR": "EST", "VL": "VILA", "VILA": "VILA", "SIT": "SITIO",
    "SITIO": "SITIO", "FAZ": "FAZ", "FAZENDA": "FAZ", "LOT": "LOT", "LOTEAMENTO": "LOT", "CONJ": "CJ",
    "CJ": "CJ", "CONJUNTO": "CJ", "DIST": "DIST", "DISTRITO": "DIST", "LOC": "LOC", "LOCALIDADE": "LOC",
    "BR": "BR", "CE": "CE",
}  # fmt: skip
# Títulos e preposições: não distinguem uma rua de outra e são omitidos ao acaso.
_TITULOS = frozenset(
    """
DR DRA DOUTOR DOUTORA CEL CORONEL DES DESEMBARGADOR DESB SEN SENADOR DEP DEPUTADO PROF PROFA PROFESSOR
PROFESSORA PE PADRE GAL GEN GENERAL CAP CAPITAO MAJ MAJOR PRES PRESIDENTE ENG ENGENHEIRO MONS MONSENHOR
SAO STA SANTA SANTO STO TEN TENENTE SGT SARGENTO MAL MARECHAL GOV GOVERNADOR VER VEREADOR PREF PREFEITO
DOM DONA D FREI IRMA ALM ALMIRANTE ENGO COM COMENDADOR BEL BACHAREL MIN MINISTRO CONEGO CON BRIG
BRIGADEIRO VISC VISCONDE BARAO DUQUE
""".split()
)
_STOP = frozenset("DE DA DO DOS DAS E N NO NR NUM NUMERO".split())
_COMPLEMENTO = {
    "SALA": "SALA", "SL": "SALA", "SALAS": "SALA", "LOJA": "LOJA", "LJ": "LOJA", "LOJAS": "LOJA",
    "APTO": "APTO", "AP": "APTO", "APT": "APTO", "APARTAMENTO": "APTO", "BLOCO": "BLOCO", "BL": "BLOCO",
    "BOX": "BOX", "ANDAR": "ANDAR", "GALPAO": "GALPAO", "CASA": "CASA", "QUADRA": "QD", "QD": "QD",
    "LOTE": "LT", "LT": "LT", "KM": "KM",
}  # fmt: skip
_NAO_E_PORTA = frozenset("BR CE KM QUADRA QD LOTE LT CONJUNTO BLOCO BL LOJA SALA APTO AP CASA".split())
_SEM_NUMERO = re.compile(r"\bS\s*/\s*N[O]?\b|\bSN\b|SEM NUMERO|\bS\s*N\b")
_CEP_NO_TEXTO = re.compile(r"(?<!\d)(\d{2})\.?(\d{3})[-. ]?(\d{3})(?!\d)")
_MAX_DIGITOS_PORTA = 6


def _sem_acento(texto: object) -> str:
    decomposto = unicodedata.normalize("NFKD", "" if texto is None else str(texto))
    return "".join(c for c in decomposto if not unicodedata.combining(c)).upper()


def _cep8(bruto: object) -> str | None:
    digitos = re.sub(r"\D", "", "" if bruto is None else str(bruto))
    return digitos if len(digitos) == 8 and len(set(digitos)) > 1 else None


@dataclass(frozen=True)
class EnderecoDeclarado:
    numero: str | None
    nome: tuple[str, ...]  # tokens do nome da rua, sem tipo, título e preposição
    complemento: frozenset[tuple[str, str]]
    cep: str | None
    municipio: str

    @property
    def cep5(self) -> str | None:
        return self.cep[:5] if self.cep else None


def analisar_endereco(texto: object, cep: object = None, municipio: object = None) -> EnderecoDeclarado:
    """Decompõe o endereço livre da 505 ('LOGRADOURO, NUMERO COMPLEMENTO    BAIRRO', ou, em 2009-2012,
    'LOGRADOURO NUM COMPL BAIRRO,CIDADE,UF, CEP 99999999') em número, nome, complemento e CEP."""
    bruto = _sem_acento(texto)
    sem_numero = bool(_SEM_NUMERO.search(bruto))
    achado = _CEP_NO_TEXTO.search(bruto)
    cep_texto = _cep8("".join(achado.groups())) if achado else None
    limpo = _CEP_NO_TEXTO.sub(" , ", bruto)
    limpo = re.sub(r"\bCEP\b", " , ", limpo)
    limpo = _SEM_NUMERO.sub(" SN ", limpo)
    segmentos = [
        re.findall(r"[A-Z]+|\d+", x) for x in re.split(r"[,;]|(?<=[A-Z0-9]{4})\.\s|\s-\s|\s{2,}", limpo)
    ]
    segmentos = [x for x in segmentos if x]
    primeiro = segmentos[0] if segmentos else []
    seguintes = segmentos[1:]
    # Número da porta: a regra da chave (primeiro número depois de uma palavra, fora de complemento e de
    # nome como '24 DE MAIO'), só no primeiro segmento; senão, o segundo pode começar pelo número.
    indice = None
    for i, tk in enumerate(primeiro):
        if i == 0 or not tk.isdigit() or primeiro[i - 1] in _NAO_E_PORTA:
            continue
        if i + 1 < len(primeiro) and primeiro[i + 1] == "DE":
            continue
        indice = i
        break
    numero = None
    if indice is not None:
        numero = primeiro[indice]
        nome, resto = primeiro[:indice], primeiro[indice + 1 :]
        if seguintes and seguintes[0][0] in _COMPLEMENTO:
            resto = resto + seguintes[0]
    else:
        nome, resto = primeiro, []
        if seguintes:
            s1 = [t for t in seguintes[0] if t not in ("N", "NO", "NR", "NUM", "NUMERO")]
            if s1 and s1[0].isdigit():
                numero = s1[0]
                resto = s1[1:] + (
                    seguintes[1] if len(seguintes) > 1 and seguintes[1][0] in _COMPLEMENTO else []
                )
        for i, tk in enumerate(nome):
            if i > 0 and (tk == "SN" or tk in _COMPLEMENTO):
                resto = nome[i:] + resto
                nome = nome[:i]
                break
    if sem_numero or (numero is not None and len(numero) > _MAX_DIGITOS_PORTA):
        numero = None
    numero = (numero.lstrip("0") or None) if numero else None
    nome = [t for t in nome if t != "SN"]
    if nome and nome[0] in _TIPOS:
        nome = nome[1:]
        if nome and nome[0] in ("R", "RUA", "AV", "AVENIDA") and len(nome) > 1:
            nome = nome[1:]
    nome_completo = [t for t in nome if t not in _STOP]
    nome = [t for t in nome_completo if t not in _TITULOS] or nome_completo
    complemento: set[tuple[str, str]] = set()
    atual = None
    for tk in resto:
        if tk in _COMPLEMENTO:
            atual = _COMPLEMENTO[tk]
        elif tk.isdigit() and atual:
            complemento.add((atual, tk.lstrip("0") or "0"))
        elif tk in ("N", "NO", "E", "NR"):
            continue
        elif atual and len(tk) == 1:
            complemento.add((atual, tk))
        else:
            atual = None
    return EnderecoDeclarado(
        numero=numero,
        nome=tuple(nome),
        complemento=frozenset(complemento),
        cep=_cep8(cep) or cep_texto,
        municipio=_sem_acento(municipio).strip(),
    )


def _jaro(s: str, t: str) -> float:
    if s == t:
        return 1.0
    ls, lt = len(s), len(t)
    if not ls or not lt:
        return 0.0
    janela = max(ls, lt) // 2 - 1
    ms, mt = [False] * ls, [False] * lt
    m = 0
    for i, c in enumerate(s):
        for j in range(max(0, i - janela), min(lt, i + janela + 1)):
            if not mt[j] and t[j] == c:
                ms[i] = mt[j] = True
                m += 1
                break
    if not m:
        return 0.0
    k = transposicoes = 0
    for i in range(ls):
        if ms[i]:
            while not mt[k]:
                k += 1
            if s[i] != t[k]:
                transposicoes += 1
            k += 1
    return (m / ls + m / lt + (m - transposicoes / 2) / m) / 3


def jaro_winkler(s: str, t: str, p: float = 0.1) -> float:
    j = _jaro(s, t)
    prefixo = 0
    for a, b in zip(s[:4], t[:4], strict=False):
        if a != b:
            break
        prefixo += 1
    return j + prefixo * p * (1 - j)


def _sim_token(a: str, b: str) -> float:
    if a == b:
        return 1.0
    if (len(a) == 1 and b.startswith(a)) or (len(b) == 1 and a.startswith(b)):
        return 0.85  # inicial abreviada: 'F' x 'FRANCISCO'
    return jaro_winkler(a, b)


def _monge_elkan(xs: tuple[str, ...], ys: tuple[str, ...]) -> float:
    return sum(max(_sim_token(x, y) for y in ys) for x in xs) / len(xs)


def similaridade_nome(a: tuple[str, ...], b: tuple[str, ...]) -> float:
    """Em [0, 1]: o maior entre Jaro-Winkler do nome inteiro (digitação: 'BNEVOLO' x 'BENEVOLO') e
    Monge-Elkan simétrico (ordem e palavra omitida)."""
    if not a or not b:
        return 0.0
    sa, sb = " ".join(a), " ".join(b)
    if sa == sb:
        return 1.0
    return max(jaro_winkler(sa, sb), (_monge_elkan(a, b) + _monge_elkan(b, a)) / 2)


def _complementos_conflitam(a: frozenset, b: frozenset) -> bool:
    """Mesmo tipo de complemento com valores diferentes, sem nenhum em comum (sala 01 x sala 02)."""
    if not a or not b or a == b or a & b:
        return False
    return bool({t for t, _ in a} & {t for t, _ in b})


LIMIAR_MESMO_CEP5 = 0.93
LIMIAR_MESMO_MUNICIPIO = 0.97


def _limiar(a: EnderecoDeclarado, b: EnderecoDeclarado) -> float | None:
    if a.cep5 and a.cep5 == b.cep5:
        return LIMIAR_MESMO_CEP5
    if a.municipio and a.municipio == b.municipio:
        return LIMIAR_MESMO_MUNICIPIO
    return None


class FamiliaEndereco:
    """Grau aproximado de um endereço: raízes de CNPJ que declararam, na 505 inteira, o mesmo número no
    mesmo CEP de 5 dígitos (ou no mesmo município) com nome de rua parecido."""

    def __init__(self) -> None:
        self._blocos: dict[tuple[str, str, str], dict[tuple[str, ...], set[str]]] = defaultdict(
            lambda: defaultdict(set)
        )
        self._cache: dict[tuple, int] = {}

    def adicionar(self, endereco: EnderecoDeclarado, raiz: str) -> None:
        if not endereco.numero:
            return
        if endereco.cep5:
            self._blocos[("C", endereco.cep5, endereco.numero)][endereco.nome].add(raiz)
        if endereco.municipio:
            self._blocos[("M", endereco.municipio, endereco.numero)][endereco.nome].add(raiz)

    def grau(self, a: EnderecoDeclarado, b: EnderecoDeclarado, limiar: float) -> int:
        bloco = ("C", a.cep5, a.numero) if limiar == LIMIAR_MESMO_CEP5 else ("M", a.municipio, a.numero)
        chave = (bloco, a.nome, b.nome)
        if chave not in self._cache:
            raizes: set[str] = set()
            for nome, donos in self._blocos.get(bloco, {}).items():
                if similaridade_nome(nome, a.nome) >= limiar or similaridade_nome(nome, b.nome) >= limiar:
                    raizes |= donos
            self._cache[chave] = len(raizes)
        return self._cache[chave]


def comparar_enderecos(
    a: EnderecoDeclarado, b: EnderecoDeclarado, familia: FamiliaEndereco, grau_exato: int = 0
) -> VinculoAproximado | None:
    """Vínculo aproximado entre dois endereços declarados, ou None. Quem chama já descartou o par cujas
    chaves exatas coincidem. `grau_exato` é o maior grau já conhecido das chaves exatas dos dois lados
    (o `grauNacional`), que também barra o prédio comercial."""
    if not a.numero or a.numero != b.numero or not a.nome or not b.nome:
        return None
    limiar = _limiar(a, b)
    if limiar is None:
        return None
    # Número de um dígito em nome de uma palavra, com CEP diferente: 'SAO COSMO, 1' x 'VILA SAO COSMO, 01'
    # é uma localidade rural com número de preenchimento (conferência manual, par E09).
    if (a.cep is None or a.cep != b.cep) and len(a.numero) < 2 and min(len(a.nome), len(b.nome)) < 2:
        return None
    similaridade = similaridade_nome(a.nome, b.nome)
    if similaridade < limiar:
        return None
    grau = max(familia.grau(a, b, limiar), grau_exato)
    if grau >= GRAU_MAXIMO:
        return None
    padrao = "mesmo_cep5" if limiar == LIMIAR_MESMO_CEP5 else "mesmo_municipio"
    if _complementos_conflitam(a.complemento, b.complemento):
        return VinculoAproximado(MESMO_PREDIO, padrao, PISTA, grau)
    return VinculoAproximado(ENDERECO_APROXIMADO, padrao, APROXIMADO, grau)
