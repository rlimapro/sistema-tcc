"""Domínios do Manual do SIM usados pelo ETL 1 (porte de `mvp-v2/pipelines/tce_sim/model/enums.py`)."""

from __future__ import annotations

import unicodedata
from enum import StrEnum


class ClasseContrato(StrEnum):
    ORIGINAL = "original"
    ACRESCIMO = "acrescimo"
    REDUCAO = "reducao"
    PRAZO = "prazo"
    RENOVACAO = "renovacao"
    DESCONHECIDA = "desconhecida"


# Campo `modalide_contrato` (com o erro de digitação da própria API).
MODALIDADE_CONTRATO: dict[str, ClasseContrato] = {
    "OR": ClasseContrato.ORIGINAL,
    "AA": ClasseContrato.ACRESCIMO,
    "AR": ClasseContrato.REDUCAO,
    "AP": ClasseContrato.PRAZO,
    "PA": ClasseContrato.ACRESCIMO,
    "PR": ClasseContrato.REDUCAO,
    "RE": ClasseContrato.RENOVACAO,
}


def classe_do_contrato(modalidade: str | None) -> ClasseContrato:
    """Código desconhecido não derruba a carga: vira DESCONHECIDA e quem chama conta."""
    return MODALIDADE_CONTRATO.get((modalidade or "").strip(), ClasseContrato.DESCONHECIDA)


# Campo nº 5 do manual, `modalidade_processo_administrativo` (tabela 501).
ESPECIE_PROCESSO = {
    "N": "Processo licitatório",
    "F": "Dispensa art. 75 I/II (baixo valor)",
    "D": "Demais dispensas",
    "I": "Inexigibilidade",
    "R": "Adesão a ata",
    "P": "Organismos internacionais",
    "C": "Credenciamento",
    "Q": "Pré-qualificação",
    "M": "Manifestação de interesse",
}
ESPECIE_LICITACAO_DE_FATO = "N"

# Campo nº 18 do manual, `modalidade_licitacao`: 1 Convite, 2 Tomada de Preços, 3 Concorrência,
# 4 Concurso, 5 Leilão, 6 Pregão, 7 RDC, 8 Diálogo Competitivo, 9 Não se aplica (a observação e.7
# manda dispensa, inexigibilidade e adesão usarem 9).
MODALIDADES_COM_DISPUTA = frozenset({"1", "2", "3", "4", "5", "6", "7", "8"})

# Adesão de verdade: `nome_orgao_ata` nomeia o órgão de origem e a ata original.
MARCA_ATA_DE_OUTRO_ORGAO = "registro de precos original"
# SRP conduzido pelo próprio município, rotulado "R" por engano na origem.
MARCA_SRP_PROPRIO = "exercicio de"


def _sem_acento_minusculo(texto: str | None) -> str:
    decomposto = unicodedata.normalize("NFKD", texto or "")
    return "".join(c for c in decomposto if not unicodedata.combining(c)).strip().lower()


def eh_licitacao_de_fato(
    especie: str | None, nome_orgao_ata: str | None, modalidade: str | None = None
) -> bool:
    """A licitação teve disputa própria (o universo do indício de conluio). Falha fechada.

    - espécie "N": sim;
    - espécie "R": só se `nome_orgao_ata` marca SRP próprio e não marca ata de outro órgão
      (v2: 1.051 de 1.594 "R" são SRP próprio rotulado errado; a marca da ata é checada primeiro,
      porque 175 adesões trazem as duas marcas);
    - espécie vazia: só com modalidade de disputa (1 a 8). Acontece só em 2009 (3.229 processos no
      cache do v2, todos com modalidade 1, 2, 3 ou 6; as dispensas de 2009 vêm com espécie "D").
      Sem essa regra o único conluio julgado do acervo (Trairi, 2009.01.20.2) ficaria de fora;
    - qualquer outra espécie, ou código desconhecido: não.
    """
    codigo = (especie or "").strip()
    if codigo == ESPECIE_LICITACAO_DE_FATO:
        return True
    if not codigo:
        return (modalidade or "").strip() in MODALIDADES_COM_DISPUTA
    if codigo != "R":
        return False
    orgao = _sem_acento_minusculo(nome_orgao_ata)
    if MARCA_ATA_DE_OUTRO_ORGAO in orgao:
        return False
    return MARCA_SRP_PROPRIO in orgao
