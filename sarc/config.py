"""Configuração do sistema, lida de um arquivo .env escolhido explicitamente.

O arquivo indicado por `--env-file` (padrão `.env`) SOBRESCREVE as variáveis já
presentes no ambiente (`override=True`). No v2, `load_dotenv()` sem override
deixava uma variável esquecida no shell apontar a carga para outro banco, sem erro
(CRITICA F-09). Por isso a URI efetiva sempre sai no log.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from functools import cached_property
from pathlib import Path

from dotenv import load_dotenv

logger = logging.getLogger(__name__)

ENV_FILE_PADRAO = ".env"
# Raiz do mvp-v3 (a pasta do pyproject.toml), achada pela localização deste pacote e não pela pasta de
# trabalho: o cliente MCP pode subir o servidor com qualquer cwd (até `/`).
RAIZ_DO_PROJETO = Path(__file__).resolve().parent.parent
# Trilha de chamadas (D-45) e Cypher livre (D-44), JSON Lines diário. Relativo à RAIZ_DO_PROJETO.
AUDITORIA_DIR_PADRAO = "logs/auditoria"


class ErroDeConfiguracao(RuntimeError):
    """Variável obrigatória ausente ou com valor inválido."""


def ler_env_file(env_file: str | Path | None = ENV_FILE_PADRAO) -> Path | None:
    """Carrega o arquivo no ambiente do processo, sobrescrevendo o que já houver.

    O padrão `.env` pode não existir (as variáveis então vêm do ambiente); um arquivo
    pedido explicitamente e inexistente é erro. Devolve o caminho lido, ou None.
    """
    if env_file is None:
        return None
    caminho = Path(env_file)
    if not caminho.is_file():
        if str(env_file) == ENV_FILE_PADRAO:
            return None
        raise ErroDeConfiguracao(f"arquivo de configuração não encontrado: {caminho}")
    load_dotenv(caminho, override=True)
    return caminho.resolve()


def _texto(nome: str, padrao: str) -> str:
    valor = os.environ.get(nome, "").strip()
    return valor or padrao


def diretorio_de_auditoria() -> Path:
    """Pasta (absoluta) dos registros de auditoria do Cypher livre, sem exigir a senha do banco.

    `AUDITORIA_DIR` absoluto (ou com `~`) vale como está; relativo, e o padrão `logs/auditoria`, são
    resolvidos a partir da raiz do mvp-v3, nunca da pasta de trabalho: assim o servidor (subido pelo
    cliente MCP com qualquer cwd) e o `sarc auditoria` sempre olham a mesma pasta.
    """
    caminho = Path(_texto("AUDITORIA_DIR", AUDITORIA_DIR_PADRAO)).expanduser()
    return caminho if caminho.is_absolute() else RAIZ_DO_PROJETO / caminho


def _inteiro(nome: str, padrao: int) -> int:
    bruto = _texto(nome, str(padrao))
    try:
        return int(bruto)
    except ValueError as erro:
        raise ErroDeConfiguracao(f"{nome} deve ser inteiro, veio {bruto!r}") from erro


def _real(nome: str, padrao: float) -> float:
    bruto = _texto(nome, str(padrao))
    try:
        return float(bruto)
    except ValueError as erro:
        raise ErroDeConfiguracao(f"{nome} deve ser número, veio {bruto!r}") from erro


@dataclass(frozen=True)
class Config:
    neo4j_uri: str
    neo4j_user: str
    neo4j_password: str = field(repr=False)
    neo4j_test_uri: str
    tce_base_url: str
    tce_cache_path: Path
    tce_page_size: int
    tce_request_delay: float
    tce_timeout_connect: float
    tce_timeout_read: float
    rfb_data_path: Path
    rfb_snapshot: str
    rfb_chunk_size: int
    grau_maximo_contato: int
    log_level: str
    log_file: str | None
    auditoria_dir: Path = RAIZ_DO_PROJETO / AUDITORIA_DIR_PADRAO
    env_file: Path | None = None

    @property
    def tce_timeout(self) -> tuple[float, float]:
        """Timeout (conexão, leitura) no formato do `requests`."""
        return (self.tce_timeout_connect, self.tce_timeout_read)

    @classmethod
    def do_ambiente(cls, env_file: Path | None = None) -> Config:
        """Monta a configuração a partir de `os.environ`. A senha não tem default."""
        senha = os.environ.get("NEO4J_PASSWORD", "").strip()
        if not senha:
            raise ErroDeConfiguracao(
                "NEO4J_PASSWORD não definido. Copie .env.example para .env e preencha, "
                "ou indique outro arquivo com --env-file."
            )
        if len(senha) < 8:
            raise ErroDeConfiguracao(
                "NEO4J_PASSWORD precisa de pelo menos 8 caracteres (exigência do Neo4j 5)."
            )
        config = cls(
            neo4j_uri=_texto("NEO4J_URI", "bolt://localhost:7699"),
            neo4j_user=_texto("NEO4J_USER", "neo4j"),
            neo4j_password=senha,
            neo4j_test_uri=_texto("NEO4J_TEST_URI", "bolt://localhost:7700"),
            tce_base_url=_texto("TCE_BASE_URL", "https://api-dados-abertos.tce.ce.gov.br/sim"),
            tce_cache_path=Path(_texto("TCE_CACHE_PATH", "./data/raw/tce_sim")),
            tce_page_size=_inteiro("TCE_PAGE_SIZE", 1000),
            tce_request_delay=_real("TCE_REQUEST_DELAY", 0.3),
            tce_timeout_connect=_real("TCE_TIMEOUT_CONNECT", 10.0),
            tce_timeout_read=_real("TCE_TIMEOUT_READ", 60.0),
            # Pasta dos snapshots (subpastas AAAA-MM); RFB_SNAPSHOT vazio = o mais recente (D-40).
            rfb_data_path=Path(_texto("RFB_DATA_PATH", "./data/rfb")),
            rfb_snapshot=_texto("RFB_SNAPSHOT", ""),
            rfb_chunk_size=_inteiro("RFB_CHUNK_SIZE", 50000),
            grau_maximo_contato=_inteiro("GRAU_MAXIMO_CONTATO", 5),
            log_level=_texto("LOG_LEVEL", "INFO").upper(),
            log_file=_texto("LOG_FILE", "") or None,
            auditoria_dir=diretorio_de_auditoria(),
            env_file=env_file,
        )
        logger.info(
            "configuração: neo4j=%s usuario=%s env-file=%s",
            config.neo4j_uri,
            config.neo4j_user,
            env_file or "(nenhum; só o ambiente)",
        )
        return config


def carregar(env_file: str | Path | None = ENV_FILE_PADRAO) -> Config:
    """Lê o arquivo (sobrescrevendo o ambiente) e devolve a configuração validada."""
    return Config.do_ambiente(ler_env_file(env_file))


@dataclass
class Contexto:
    """Objeto do click (`ctx.obj`): a configuração só é montada quando um comando a pede,
    para que `--help` funcione sem `.env`."""

    env_file: Path | None

    @cached_property
    def config(self) -> Config:
        return Config.do_ambiente(self.env_file)
