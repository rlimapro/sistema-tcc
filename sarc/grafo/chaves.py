"""Chaves determinísticas do grafo (porte de `mvp-v2/graph/keys.py`).

Cada chave é montada em lugares distantes: o ETL 1 cria o nó com ela, e outra etapa
(o `/contratados`, o ETL 2, o contato declarado) monta a mesma string a partir de outros
campos. Divergência entre as duas montagens quebra arestas sem erro nenhum, por isso
toda normalização fica aqui, num só lugar, com teste sobre o formato real da fonte.
"""

from __future__ import annotations

import re
import unicodedata
import uuid

_UUID_NS = uuid.UUID("12345678-1234-5678-1234-567812345678")


def _n(valor: object) -> str:
    """Normalização mínima: texto sem espaços nas pontas (a fonte preenche campos com espaços)."""
    return "" if valor is None else str(valor).strip()


def _alfanum(valor: object) -> str:
    """Só letras e dígitos, em maiúsculas. Casa `numero_processo_adm` (/contratos) com
    `numero_licitacao` (/processos) mesmo quando vêm pontuados de jeito diferente."""
    return re.sub(r"[^A-Za-z0-9]", "", _n(valor)).upper()


def _dia(valor: object) -> str:
    """'2026-01-09T00:00:00' -> '2026-01-09'."""
    return _n(valor)[:10]


def _sem_acento(texto: object) -> str:
    decomposto = unicodedata.normalize("NFKD", _n(texto))
    return "".join(c for c in decomposto if not unicodedata.combining(c)).upper()


def id_municipio(codigo_municipio: object) -> str:
    """Código do TCE com zero à esquerda ('087'); é texto, nunca número."""
    return _n(codigo_municipio)


def id_licitacao(codigo_municipio: object, numero_licitacao: object) -> str:
    """Ex.: '144|160022026PERP'.

    A chave (município, número normalizado) colide: a mesma numeração é reaproveitada por
    processos distintos (v2: 42 de 981 chaves em 144/2020-2026). A colisão é resolvida e
    marcada no transformador (`chaveColidida`), não aqui.
    """
    return f"{_n(codigo_municipio)}|{_alfanum(numero_licitacao)}"


def id_contrato(
    codigo_municipio: object,
    numero_contrato: object,
    data_contrato: object,
    numero_original: object = None,
    data_original: object = None,
) -> str:
    """Ex. original: '144|20260011|2026-01-09'.
    Ex. aditivo: '144|2014.03.24-0002|2014-10-14#2014.03.24-0015|2014-03-24'.

    A tripla (município, número, data) não identifica um aditivo: a fonte reaproveita o mesmo
    par para reduções e acréscimos de um lote. Por isso o aditivo leva o par do original como
    sufixo. **Só o aditivo**: quem chama passa `numero_original` apenas quando a classe não é
    original (há municípios que preenchem o campo no próprio original; v2, 2.426 órfãos falsos).
    """
    base = f"{_n(codigo_municipio)}|{_n(numero_contrato)}|{_dia(data_contrato)}"
    numero_original_n = _n(numero_original)
    if not numero_original_n:
        return base
    return f"{base}#{numero_original_n}|{_dia(data_original)}"


def gerar_id_pessoa(cpf_mascarado: object, nome_socio: object) -> str:
    """RFB. O nome desambigua colisões nos seis dígitos visíveis do CPF mascarado."""
    return str(uuid.uuid5(_UUID_NS, f"{_n(cpf_mascarado)}|{_n(nome_socio).upper()}"))


def cnpj_valido(documento: object) -> str | None:
    """CNPJ só com dígitos e exatamente 14 deles, ou None.

    Um único documento vazio viraria um nó `Estabelecimento {cnpj: ''}`, hub falso de
    conluio (CRITICA F-23). Quem chama conta e descarta o None.
    """
    digitos = re.sub(r"\D", "", _n(documento))
    if len(digitos) != 14 or len(set(digitos)) == 1:
        return None
    return digitos


def cnpj_dv_confere(cnpj: str) -> bool:
    """Os dois dígitos verificadores do CNPJ (14 dígitos) conferem. O ETL 1 só conta os que não
    conferem, sem descartar: o erro de digitação vira um nó isolado, que não casa com a RFB."""

    def _dv(base: str) -> str:
        pesos = list(range(len(base) - 7, 1, -1)) + list(range(9, 1, -1))
        resto = sum(int(d) * p for d, p in zip(base, pesos, strict=True)) % 11
        return "0" if resto < 2 else str(11 - resto)

    return len(cnpj) == 14 and cnpj.isdigit() and _dv(cnpj[:12]) == cnpj[12] and _dv(cnpj[:13]) == cnpj[13]


# ---------------------------------------------------------------------------
# Contato (telefone e endereço). Usadas pelo ETL 1 (tabela 505, contato declarado no
# certame) e pelo ETL 2 (RFB), para que o mesmo contato caia no mesmo nó.
# ---------------------------------------------------------------------------


def chave_telefone(bruto: object) -> str | None:
    """Telefone como DDD + número, só dígitos (10 ou 11), ou None.

    Tira o zero de tronco ('08834120445' -> '8834120445') e o 55 do país. Rejeita número sem
    DDD (8 ou 9 dígitos: não dá para saber a cidade) e número de dígito repetido ('0000000000').
    Na RFB, passe DDD e telefone concatenados.
    """
    digitos = re.sub(r"\D", "", _n(bruto)).lstrip("0")
    if digitos.startswith("55") and len(digitos) in (12, 13):
        digitos = digitos[2:]
    if len(digitos) not in (10, 11) or len(set(digitos[2:])) == 1:
        return None
    return digitos


def _cep(bruto: object) -> str | None:
    digitos = re.sub(r"\D", "", _n(bruto))
    if len(digitos) != 8 or len(set(digitos)) == 1:
        return None
    return digitos


_SEM_NUMERO = re.compile(r"\bS\s*/\s*N\b|\bSN\b|SEM NUMERO")
# CEP escrito no endereço livre ('CEP61600040', 'CEP 60165-011', 'MILAGRES. CE. 63.250.000'). Sai do texto
# antes de procurar o número da porta: sem isso, 17.400 declarações da 505 tinham o CEP (ou um trecho dele)
# como número, e empresas de ruas diferentes do mesmo CEP genérico caíam no mesmo `Endereco`.
_CEP_NO_TEXTO = re.compile(r"\bCEP\b[\s:.-]*(?:\d[\d.-]*)?|(?<!\d)\d{2}\.?\d{3}[-. ]?\d{3}(?!\d)")
# Número da porta com 7 dígitos ou mais não existe: é CEP sem pontuação ou telefone.
_MAX_DIGITOS_PORTA = 6
# Palavra que, antes de um número, indica que ele não é o número da porta.
_NAO_E_PORTA = frozenset(
    {
        "BR",
        "CE",
        "KM",
        "QUADRA",
        "QD",
        "LOTE",
        "LT",
        "CONJUNTO",
        "BLOCO",
        "BL",
        "LOJA",
        "SALA",
        "APTO",
        "AP",
        "CASA",
    }
)


def numero_do_endereco(endereco: object) -> str | None:
    """Número da porta extraído do endereço livre da tabela 505, ou None.

    Regra: o primeiro número depois de ao menos uma palavra, exceto quando ele faz parte do
    nome ('RUA 1 DE MAIO', 'BR 116', 'KM 5') ou é complemento ('LOJA 05', 'SALA 01').
    Formatos reais: 'RUA ZEZITO GOMES, 411 SALA 01', 'R ZEZITO GOMES N 411',
    'AV MONSENHOR TABOSA 1001 LOJA N 05'. S/N devolve None (sem chave: falha fechada). O CEP escrito
    no texto é retirado antes ('RUA X,CENTRO,CEP62580000' não tem número, e não o 62580000).
    """
    texto = _sem_acento(endereco)
    if _SEM_NUMERO.search(texto):
        return None
    tokens = re.findall(r"[A-Z]+|\d+", _CEP_NO_TEXTO.sub(" ", texto))
    for i, token in enumerate(tokens):
        if i == 0 or not token.isdigit() or len(token) > _MAX_DIGITOS_PORTA:
            continue
        if tokens[i - 1] in _NAO_E_PORTA:
            continue
        if i + 1 < len(tokens) and tokens[i + 1] == "DE":
            continue
        return token.lstrip("0") or None
    return None


# Tipos de logradouro, preposições e títulos: pulados para achar a palavra que nomeia a rua
# ('RUA DR. MOREIRA DA ROCHA' e 'RUA MOREIRA DA ROCHA' dão os dois 'MOREIRA').
_NAO_NOMEIA_RUA = frozenset(
    """
    R RUA AV AVENIDA TRAV TR TV TRAVESSA ROD RODOVIA AL ALAMEDA PC PCA PRACA ESTRADA EST N NO DE DA DO DOS DAS
    DR DOUTOR CEL CORONEL DES DESEMBARGADOR DESB SEN SENADOR DEP DEPUTADO PROF PROFESSOR PROFESSORA PE PADRE
    GAL GEN GENERAL CAP CAPITAO MAJ MAJOR PRES PRESIDENTE ENG ENGENHEIRO MONS MONSENHOR SAO STA SANTA SANTO
    """.split()
)


def primeira_palavra_do_logradouro(logradouro: object) -> str:
    for palavra in re.findall(r"[A-Z]+", _sem_acento(logradouro)):
        if len(palavra) > 1 and palavra not in _NAO_NOMEIA_RUA:
            return palavra
    return ""


def chave_endereco(cep: object, numero: object, logradouro: object) -> str | None:
    """Chave do nó `Endereco`, ou None (sem CEP válido ou sem número).

    - CEP específico (não termina em 000): 'cep|numero'. O CEP já identifica o trecho de rua.
    - CEP genérico (termina em 000, o CEP de uma cidade pequena inteira): 'cep|numero|palavra',
      com a primeira palavra que nomeia a rua. Sem ela, '62720000|175' juntaria ruas diferentes
      (medido no cache do v2: 'RUA JOSE SANTOS FILHO, 175' e 'R FRANCISCA DE ASSIS UCHOA, 175').

    `numero` pode vir pronto (RFB) ou ser extraído do endereço livre (`numero_do_endereco`).
    """
    cep_n = _cep(cep)
    numero_n = re.sub(r"\D", "", _n(numero)).lstrip("0")
    if not cep_n or not numero_n:
        return None
    if cep_n.endswith("000"):
        return f"{cep_n}|{numero_n}|{primeira_palavra_do_logradouro(logradouro)}"
    return f"{cep_n}|{numero_n}"
