"""Qualificações do quadro societário (tabela `Qualificacoes` da RFB).

Constante única, usada pelo ETL 2 e por toda consulta de conluio e tool do MCP (CRITICA F-11).
No v2 a lista ficava em `config/settings.py` e parte das consultas não a aplicava.

Ficam FORA, de propósito:
- 17 Procurador: em geral contador ou despachante, que representa muitas empresas sem ser dono;
- 12 Inventariante e 09 Curador: papel temporário de quem administra bens de outra pessoa;
- 14 Mãe, 15 Pai, 35 Tutor: parentesco ou tutela do sócio representado (nem viram aresta).

As qualificações de condição pessoal (menoridade, incapacidade) são mascaradas para 22 no
transformador, por privacidade, e por isso não aparecem no grafo.
"""

from __future__ import annotations

QUALIFICACOES_SOCIETARIAS: frozenset[str] = frozenset(
    {
        "05",  # Administrador
        "08",  # Conselheiro de Administração
        "10",  # Diretor
        "16",  # Presidente
        "22",  # Sócio
        "37",  # Sócio Pessoa Jurídica Domiciliado no Exterior
        "49",  # Sócio-Administrador
        "52",  # Sócio com Capital
        "53",  # Sócio sem Capital
        "65",  # Titular Pessoa Física Residente ou Domiciliado no Brasil
    }
)

QUALIFICACOES_EXCLUIDAS: frozenset[str] = frozenset({"09", "12", "17"})

# Condição da pessoa (menoridade, incapacidade civil), não posição societária. Mascaradas para
# 22 por colisão: 22 é a qualificação mais comum, e o caso mascarado fica indistinguível.
QUALIFICACOES_CONDICAO_PESSOAL: frozenset[str] = frozenset(
    {
        "29",  # Sócio Incapaz ou Relat.Incapaz (exceto menor)
        "30",  # Sócio Menor (Assistido/Representado)
        "58",  # Sócio Comanditário Incapaz
        "67",  # Titular Pessoa Física Incapaz ou Relativamente Incapaz (exceto menor)
        "68",  # Titular Pessoa Física Menor (Assistido/Representado)
    }
)
QUALIFICACAO_MASCARA = "22"

# Qualificação do REPRESENTANTE que descreve parentesco ou tutela sobre o representado. A aresta
# REPRESENTA não é emitida: ela sozinha já revelaria a menoridade ou a curatela do sócio.
QUALIFICACOES_REPRESENTANTE_PESSOAIS: frozenset[str] = frozenset(
    {
        "09",  # Curador
        "14",  # Mãe
        "15",  # Pai
        "35",  # Tutor
    }
)


def e_societaria(codigo: str | None) -> bool:
    """True se a qualificação conta como vínculo societário nas consultas de conluio."""
    return codigo in QUALIFICACOES_SOCIETARIAS
