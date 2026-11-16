"""Leitura dos CSVs da Receita Federal (dados abertos do CNPJ).

Formato confirmado nos arquivos de 2026-08: sem cabeçalho, `;` como separador, todos os campos
entre aspas (os vazios chegam como `""`), `latin-1`. Cada partição (`Socios0`..`Socios9`) é um
diretório com um único arquivo de nome variável (`K3241.K03200Y0.D60808.SOCIOCSV`).

O filtro pelos CNPJs do grafo é feito na linha crua, antes do parse: toda linha começa com
`"<cnpj_basico de 8 dígitos>"`, então `linha[1:9]` é a raiz. Só as linhas que passam vão ao
`csv`. Isso lê os 28 milhões de linhas de `Socios` de 2026-08 em cerca de 20 s (sondagem T05).
Uma linha com número de campos diferente do esperado é contada e descartada, nunca corrigida.
"""

from __future__ import annotations

import csv
import logging
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

COLS_EMPRESAS = (
    "cnpj_basico",
    "razao_social",
    "natureza_juridica",
    "qualificacao_responsavel",
    "capital_social",
    "porte_empresa",
    "ente_federativo_responsavel",
)

COLS_ESTABELECIMENTOS = (
    "cnpj_basico",
    "cnpj_ordem",
    "cnpj_dv",
    "identificador_matriz_filial",
    "nome_fantasia",
    "situacao_cadastral",
    "data_situacao_cadastral",
    "motivo_situacao_cadastral",
    "nome_cidade_exterior",
    "pais",
    "data_inicio_atividade",
    "cnae_principal",
    "cnaes_secundarios",
    "tipo_logradouro",
    "logradouro",
    "numero",
    "complemento",
    "bairro",
    "cep",
    "uf",
    "municipio",
    "ddd1",
    "telefone1",
    "ddd2",
    "telefone2",
    "ddd_fax",
    "fax",
    "correio_eletronico",
    "situacao_especial",
    "data_situacao_especial",
)

COLS_SOCIOS = (
    "cnpj_basico",
    "identificador_socio",
    "nome_socio",
    "cpf_cnpj_socio",
    "qualificacao_socio",
    "data_entrada_sociedade",
    "pais",
    "representante_legal",
    "nome_representante",
    "qualificacao_representante",
    "faixa_etaria",
)

COLUNAS_POR_TIPO: dict[str, tuple[str, ...]] = {
    "Empresas": COLS_EMPRESAS,
    "Estabelecimentos": COLS_ESTABELECIMENTOS,
    "Socios": COLS_SOCIOS,
}

N_PARTICOES = 10
ENCODING = "latin-1"


@dataclass
class EstatisticasLeitura:
    linhas_lidas: int = 0
    linhas_filtradas: int = 0
    linhas_malformadas: int = 0

    def somar(self, outra: EstatisticasLeitura) -> None:
        self.linhas_lidas += outra.linhas_lidas
        self.linhas_filtradas += outra.linhas_filtradas
        self.linhas_malformadas += outra.linhas_malformadas


def tipo_do_arquivo(nome: str) -> str:
    """`Socios3` -> `Socios`. Levanta ValueError para nome desconhecido."""
    for tipo in COLUNAS_POR_TIPO:
        if nome.startswith(tipo) and nome[len(tipo) :].isdigit():
            return tipo
    raise ValueError(f"arquivo da RFB desconhecido: {nome!r}; use EmpresasN, EstabelecimentosN ou SociosN")


def todos_os_arquivos(tipo: str) -> list[str]:
    return [f"{tipo}{i}" for i in range(N_PARTICOES)]


def localizar_arquivo(raiz: Path, nome: str) -> Path:
    """Devolve o único arquivo dentro de `raiz/nome` (o nome real muda a cada snapshot)."""
    diretorio = Path(raiz) / nome
    if not diretorio.is_dir():
        raise FileNotFoundError(f"diretório da RFB não encontrado: {diretorio}")
    arquivos = [f for f in diretorio.iterdir() if f.is_file()]
    if len(arquivos) != 1:
        raise RuntimeError(f"esperado 1 arquivo em {diretorio}, há {len(arquivos)}")
    return arquivos[0]


def ler_filtrado(
    caminho: Path,
    colunas: tuple[str, ...],
    raizes: frozenset[str] | None,
    stats: EstatisticasLeitura,
    max_linhas: int | None = None,
) -> Iterator[dict[str, str]]:
    """Linhas do arquivo como dicionários, só as de `cnpj_basico` em `raizes` (None = todas).

    `max_linhas` limita as linhas LIDAS do arquivo (modo amostra), não as que passam no filtro.
    """
    n_colunas = len(colunas)
    with open(caminho, encoding=ENCODING, newline="") as arquivo:
        for linha in arquivo:
            if max_linhas is not None and stats.linhas_lidas >= max_linhas:
                break
            stats.linhas_lidas += 1
            if raizes is not None and linha[1:9] not in raizes:
                continue
            campos = next(csv.reader([linha], delimiter=";", quotechar='"'), [])
            if len(campos) != n_colunas:
                stats.linhas_malformadas += 1
                logger.debug("linha malformada em %s: %r", caminho.name, linha[:120])
                continue
            stats.linhas_filtradas += 1
            yield dict(zip(colunas, campos, strict=True))


def em_lotes[T](itens: Iterable[T], tamanho: int) -> Iterator[list[T]]:
    lote: list[T] = []
    for item in itens:
        lote.append(item)
        if len(lote) >= tamanho:
            yield lote
            lote = []
    if lote:
        yield lote


def ler_dominio(raiz: Path, nome: str) -> dict[str, str]:
    """Tabela de domínio de arquivo único (`Municipios`, `Qualificacoes`): código -> descrição."""
    caminho = localizar_arquivo(raiz, nome)
    with open(caminho, encoding=ENCODING, newline="") as arquivo:
        return {r[0]: r[1] for r in csv.reader(arquivo, delimiter=";", quotechar='"') if len(r) >= 2}
