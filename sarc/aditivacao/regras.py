"""Aditivação: regras puras que classificam um contrato original a partir dos seus aditivos (D-35).

O indício: o contrato recebeu acréscimos de valor acima do teto legal de alteração unilateral
(Lei 8.666/1993, art. 65, §§ 1º e 2º; Lei 14.133/2021, art. 125): 25%, ou 50% só para acréscimo em
reforma de edifício ou de equipamento. É indício, não prova: a fonte não traz o termo aditivo físico.

Regras (proposta em `docs/propostas/aditivacao.md`):
- o valor do aditivo é **delta**, nunca o total acumulado (decisão 5 do v2);
- a taxa que decide é **isolada**: Σ `AA` / valor original, sem compensar supressões (o TCE-CE julga
  assim, Acórdão 6313/2024; e a fonte não preenche o valor de 84% dos `AR`);
- `AA` cuja vigência passa a maior vigência anterior é renovação lançada como acréscimo: conta como `PA`,
  só na taxa ampla (Morrinhos, Acopiara);
- `AP` e `RE` (prazo, art. 57 II da 8.666 e arts. 106-107 da 14.133), `AR`/`PR` e `AA` negativo (redução
  lançada com código de acréscimo) ficam fora do numerador e são devolvidos à parte;
- limite pelo texto do objeto do original, por palavra inteira; `indefinido` nunca recebe 50%;
- sem valor na fonte não é "dentro do limite": é excluído e contado (Icó).
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass, field
from datetime import date
from typing import Any

# Níveis e motivos. O vocabulário é o do conluio (D-12): principal e auxiliar nunca se somam.
PRINCIPAL = "principal"
AUXILIAR = "auxiliar"
DENTRO = "dentro"
EXCLUIDO = "excluido"

MOTIVO_LIMITE_INDEFINIDO = "limite_indefinido"
MOTIVO_DEPENDE_DE_REAJUSTE = "depende_de_reajuste"
MOTIVO_AA_MAIOR_QUE_ORIGINAL = "aa_maior_que_original"
MOTIVO_ADESAO_ATA = "adesao_ata"
MOTIVO_SO_NA_AMPLA = "so_na_ampla"

MOTIVO_VALOR_ESPURIO = "valor_espurio"
MOTIVO_VALOR_DIVERGENTE = "valor_divergente"
MOTIVO_ORIGINAL_SEM_VALOR = "original_sem_valor"
MOTIVO_ACRESCIMO_NAO_QUANTIFICADO = "acrescimo_nao_quantificado"

LIMITE_25 = "limite_25"
LIMITE_50 = "limite_50"
LIMITE_INDEFINIDO = "indefinido"
TETOS = {LIMITE_25: 0.25, LIMITE_50: 0.50}
TETO_MINIMO = 0.25

# Modalidades (campo `modalide_contrato`) que carregam valor de acréscimo ou de supressão.
ACRESCIMO = "AA"
PRAZO_E_ACRESCIMO = "PA"
SUPRESSOES = frozenset({"AR", "PR"})
PRAZO_OU_RENOVACAO = frozenset({"AP", "RE"})
MODALIDADES_DE_VALOR = frozenset({ACRESCIMO, PRAZO_E_ACRESCIMO}) | SUPRESSOES

# Destino de cada aditivo no cálculo.
ENTRA_ACRESCIMO = "acrescimo"  # numerador da taxa que decide
ENTRA_AMPLA = "ampla"  # só na taxa ampla (PA, AA que prorroga)
FORA_SUPRESSAO = "supressao_informativa"  # AR, PR, AA negativo
FORA_PRAZO = "prazo_ou_renovacao"  # AP, RE
FORA_DESCONHECIDA = "modalidade_desconhecida"

# Limite legal pelo objeto (porte de `mvp-v2/analysis/indicadores.py:32-38`). Padrões por palavra
# inteira: por substring, "quadra" casava "enquadramento" e "escola" casava "ônibus escolares".
TERMOS_REFORMA = (r"reformas?", r"reformar", r"reparos?", r"readequacao", r"requalificacao")
TERMOS_EDIFICACAO = (
    r"edificios?",
    r"predios?",
    r"escolas?",
    r"unidades? escolar(es)?",
    r"creches?",
    r"postos? de saude",
    r"unidades? basicas?",
    r"hospita(l|is)",
    r"sedes?",
    r"camaras?",
    r"ginasios?",
    r"quadras?",
    r"mercados?",
    r"cemiterios?",
    r"equipamentos?",
)
_REFORMA = re.compile(r"\b(" + "|".join(TERMOS_REFORMA) + r")\b")
_EDIFICACAO = re.compile(r"\b(" + "|".join(TERMOS_EDIFICACAO) + r")\b")

# Reajuste e reequilíbrio não são acréscimo de escopo (8.666, art. 65, § 8º; 14.133, art. 136), mas a
# fonte os lança como `AA`. Texto livre não classifica, só levanta hipótese: rebaixa para o auxiliar.
_REAJUSTE = re.compile(r"reajust|reequilib|repactu|realinhamento|\bigp-?m\b|\binpc\b|\bipca\b")

# Adesão a ata: as três marcas juntas (v2, `eh_adesao_ata`); a terceira tolera plural e truncamento.
_REGISTRO_DE_PRECOS = re.compile(r"registros?\s+de\s+prec")

# Lei 14.133/2021 publicada em 01/04/2021; até a revogação da 8.666 as duas conviveram (art. 191) e o
# contrato segue a lei escolhida no edital (art. 190). O SIM não informa qual foi.
INICIO_LEI_14133 = date(2021, 4, 1)
FUNDAMENTO_8666 = "Lei 8.666/1993, art. 65, §§ 1º e 2º"
FUNDAMENTO_CONVIVENCIA = (
    "Lei 8.666/1993, art. 65, § 1º, ou Lei 14.133/2021, art. 125, conforme o edital ou o contrato "
    "(arts. 190 e 191 da Lei 14.133/2021; o SIM não informa o regime)"
)


def normalizar_texto(texto: str | None) -> str:
    """Sem acento, minúsculo, espaços colapsados."""
    decomposto = unicodedata.normalize("NFKD", texto or "")
    sem_acento = "".join(c for c in decomposto if not unicodedata.combining(c))
    return " ".join(sem_acento.lower().split())


def classificar_limite(objeto: str | None) -> str:
    """`limite_50` só com termo de reforma **e** de edificação ou equipamento; `indefinido` com reforma
    sem edificação, ou objeto vazio (vai a revisão, nunca recebe 50% por omissão); `limite_25` no resto.
    Manutenção, obra nova e pavimentação ficam em 25% (D-35; decisão 7 do v2)."""
    texto = normalizar_texto(objeto)
    if not texto:
        return LIMITE_INDEFINIDO
    if not _REFORMA.search(texto):
        return LIMITE_25
    return LIMITE_50 if _EDIFICACAO.search(texto) else LIMITE_INDEFINIDO


def menciona_reajuste(texto: str | None) -> bool:
    return _REAJUSTE.search(normalizar_texto(texto)) is not None


def eh_adesao_ata(objeto: str | None) -> bool:
    texto = normalizar_texto(objeto)
    return "adesao" in texto and "ata" in texto and _REGISTRO_DE_PRECOS.search(texto) is not None


def fundamento_legal(data_contrato: date | None) -> str:
    """A citação do teto pela data do contrato. Antes da 14.133, só a 8.666; depois, as duas (mesmo teto)."""
    if data_contrato is not None and data_contrato < INICIO_LEI_14133:
        return FUNDAMENTO_8666
    return FUNDAMENTO_CONVIVENCIA


def _sem_valor(valor: float | None) -> bool:
    return valor is None or valor == 0


# --------------------------------------------------------------------------------------------
@dataclass
class AditivoAvaliado:
    id: str
    numero: str | None
    modalidade: str | None
    dataContrato: date | None
    dataFimVigencia: date | None
    valor: float | None
    objeto: str | None
    estendeVigencia: bool  # passa a maior vigência do original e dos aditivos de data estritamente anterior
    fracaoDoOriginal: float | None  # valor / valor original
    mencionaReajuste: bool | None  # None: texto não avaliado (ausente, ou não trafegou num contrato `dentro`)
    entraEm: str  # acrescimo, ampla, supressao_informativa, prazo_ou_renovacao, modalidade_desconhecida


@dataclass
class AvaliacaoContrato:
    """O veredito de um contrato original. Taxas e excesso valem `None` quando o contrato é excluído."""

    id: str
    municipio: str | None
    numero: str | None
    dataContrato: date | None
    objeto: str | None
    valorOriginal: float | None
    nivel: str  # principal, auxiliar, dentro, excluido
    motivo: str | None
    limiteAplicavel: str | None = None
    teto: float | None = None
    fundamentoLegal: str | None = None
    taxaAcrescimo: float | None = None  # Σ AA puro / original: decide o veredito
    taxaAmpla: float | None = None  # (AA puro + AA que prorroga + PA) / original
    taxaSemReajuste: float | None = None  # AA puro sem texto de reajuste / original
    excessoReais: float | None = None  # acima do teto, pela taxa que estourou
    somaAcrescimo: float = 0.0
    somaAcrescimoQueProrroga: float = 0.0
    somaPA: float = 0.0
    somaSupressoesInformativas: float = 0.0  # AR + PR + |AA negativo|, nunca compensadas
    somaPrazoRenovacao: float = 0.0  # AP + RE, fora por fundamento legal
    nAditivosNegativos: int = 0
    nAditivosSemValor: int = 0
    fornecedores: list[dict[str, Any]] = field(default_factory=list)
    licitacaoOrigem: str | None = None
    aditivos: list[AditivoAvaliado] = field(default_factory=list)

    def para_dicionario(self) -> dict[str, Any]:
        return asdict(self)


def _aditivos_em_ordem(aditivos: Iterable[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    return sorted(aditivos, key=lambda a: (a.get("dataContrato") or date.max, a.get("id") or ""))


def avaliar(
    original: Mapping[str, Any],
    aditivos: Iterable[Mapping[str, Any]],
    fornecedores: list[dict[str, Any]] | None = None,
    licitacao_origem: str | None = None,
) -> AvaliacaoContrato:
    """Classifica um contrato original. `original` e cada aditivo têm as chaves do nó `Contrato`:
    `id`, `numero`, `municipio`, `dataContrato`, `dataFimVigencia`, `modalidade`, `valorTotal`,
    `valorEspurio`, `valorDivergente`, `objeto`."""
    lista = _aditivos_em_ordem(aditivos)
    valor_original = original.get("valorTotal")
    avaliacao = AvaliacaoContrato(
        id=original["id"],
        municipio=original.get("municipio"),
        numero=original.get("numero"),
        dataContrato=original.get("dataContrato"),
        objeto=original.get("objeto"),
        valorOriginal=valor_original,
        nivel=EXCLUIDO,
        motivo=None,
        fornecedores=fornecedores or [],
        licitacaoOrigem=licitacao_origem,
    )

    # Exclusões: falha fechada, marca ausente conta como presente.
    todos = [original, *lista]
    if any(c.get("valorEspurio") is not False for c in todos):
        avaliacao.motivo = MOTIVO_VALOR_ESPURIO
    elif any(c.get("valorDivergente") is not False for c in todos):
        avaliacao.motivo = MOTIVO_VALOR_DIVERGENTE
    elif valor_original is None or valor_original <= 0:
        avaliacao.motivo = MOTIVO_ORIGINAL_SEM_VALOR
    else:
        acrescimos = [a for a in lista if a.get("modalidade") in (ACRESCIMO, PRAZO_E_ACRESCIMO)]
        if acrescimos and all(_sem_valor(a.get("valorTotal")) for a in acrescimos):
            avaliacao.motivo = MOTIVO_ACRESCIMO_NAO_QUANTIFICADO

    # A vigência de comparação só avança ao mudar de data: aditivos do mesmo dia não se afetam, e o
    # resultado não depende da ordem dos ids (um AP e um AA do mesmo dia, com a mesma vigência nova).
    vigencia = original.get("dataFimVigencia")
    vigencia_do_dia = vigencia
    dia_corrente: object = object()
    soma_sem_reajuste = 0.0
    aa_maior = False
    for a in lista:
        modalidade = a.get("modalidade")
        valor = a.get("valorTotal")
        fim = a.get("dataFimVigencia")
        if a.get("dataContrato") != dia_corrente:
            dia_corrente = a.get("dataContrato")
            vigencia = vigencia_do_dia
        estende = fim is not None and vigencia is not None and fim > vigencia
        reajuste = None if a.get("objeto") is None else menciona_reajuste(a.get("objeto"))
        if _sem_valor(valor):
            avaliacao.nAditivosSemValor += modalidade in MODALIDADES_DE_VALOR
        v = valor or 0.0
        if modalidade == ACRESCIMO and v < 0:
            destino = FORA_SUPRESSAO
            avaliacao.nAditivosNegativos += 1
            avaliacao.somaSupressoesInformativas += -v
        elif modalidade == ACRESCIMO and estende:
            destino = ENTRA_AMPLA
            avaliacao.somaAcrescimoQueProrroga += v
        elif modalidade == ACRESCIMO:
            destino = ENTRA_ACRESCIMO
            avaliacao.somaAcrescimo += v
            # Sem texto (None) conta como sem reajuste: falha para o lado do alerta.
            if not reajuste:
                soma_sem_reajuste += v
            if valor_original and v >= valor_original:
                aa_maior = True
        elif modalidade == PRAZO_E_ACRESCIMO:
            destino = ENTRA_AMPLA
            avaliacao.somaPA += v
        elif modalidade in SUPRESSOES:
            destino = FORA_SUPRESSAO
            avaliacao.somaSupressoesInformativas += abs(v)  # sinal pelo código, nunca pelo campo
        elif modalidade in PRAZO_OU_RENOVACAO:
            destino = FORA_PRAZO
            avaliacao.somaPrazoRenovacao += v
        else:
            destino = FORA_DESCONHECIDA
        if fim is not None and (vigencia_do_dia is None or fim > vigencia_do_dia):
            vigencia_do_dia = fim
        avaliacao.aditivos.append(
            AditivoAvaliado(
                id=a["id"],
                numero=a.get("numero"),
                modalidade=modalidade,
                dataContrato=a.get("dataContrato"),
                dataFimVigencia=fim,
                valor=valor,
                objeto=a.get("objeto"),
                estendeVigencia=estende,
                fracaoDoOriginal=(v / valor_original) if valor_original and valor_original > 0 else None,
                mencionaReajuste=reajuste,
                entraEm=destino,
            )
        )

    if avaliacao.motivo is not None:  # excluído: sem taxa nem veredito
        return avaliacao

    assert valor_original is not None
    limite = classificar_limite(original.get("objeto"))
    teto = TETOS.get(limite)
    taxa = avaliacao.somaAcrescimo / valor_original
    ampla = (avaliacao.somaAcrescimo + avaliacao.somaAcrescimoQueProrroga + avaliacao.somaPA) / valor_original
    avaliacao.limiteAplicavel = limite
    avaliacao.teto = teto
    avaliacao.fundamentoLegal = fundamento_legal(original.get("dataContrato"))
    avaliacao.taxaAcrescimo = taxa
    avaliacao.taxaAmpla = ampla
    avaliacao.taxaSemReajuste = soma_sem_reajuste / valor_original

    teto_efetivo = teto if teto is not None else TETO_MINIMO
    if taxa <= TETO_MINIMO and ampla <= TETO_MINIMO:
        avaliacao.nivel, avaliacao.motivo = DENTRO, None
    elif teto is None:
        avaliacao.nivel, avaliacao.motivo = AUXILIAR, MOTIVO_LIMITE_INDEFINIDO
    elif taxa > teto:
        if eh_adesao_ata(original.get("objeto")):
            avaliacao.nivel, avaliacao.motivo = AUXILIAR, MOTIVO_ADESAO_ATA
        elif aa_maior:
            avaliacao.nivel, avaliacao.motivo = AUXILIAR, MOTIVO_AA_MAIOR_QUE_ORIGINAL
        elif avaliacao.taxaSemReajuste <= teto:
            avaliacao.nivel, avaliacao.motivo = AUXILIAR, MOTIVO_DEPENDE_DE_REAJUSTE
        else:
            avaliacao.nivel, avaliacao.motivo = PRINCIPAL, None
    elif ampla > teto:
        avaliacao.nivel, avaliacao.motivo = AUXILIAR, MOTIVO_SO_NA_AMPLA
    else:
        avaliacao.nivel, avaliacao.motivo = DENTRO, None

    if avaliacao.nivel != DENTRO:
        taxa_que_estourou = taxa if taxa > teto_efetivo else ampla
        avaliacao.excessoReais = (taxa_que_estourou - teto_efetivo) * valor_original
    return avaliacao
