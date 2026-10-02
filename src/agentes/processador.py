"""
Ponto único que o botão "Processar entrada" do painel web chama
(2026-09-30). Decide ONDE o processamento roda pela variável PROCESSADOR:

- "local" (padrão): roda neste mesmo processo, lendo a pasta entrada/ do PC.
- "azure": vai fazer a requisição para o serviço no Azure. Ainda não
  existe - depende de a entrada estar no SharePoint (o Azure não enxerga
  as pastas deste PC) e de definir como a página se autentica no serviço.

A página só conhece processar_entrada(); trocar de local para Azure é mudar
a variável, sem mexer na tela.
"""

from __future__ import annotations

import threading

from filelock import FileLock, Timeout

from src.core import config
from src.agentes import monitor_pasta

logger = config.get_logger(__name__)

# Duas travas: a de thread cobre duas pessoas clicando ao mesmo tempo (o
# painel atende cada requisição numa thread do mesmo processo); a de arquivo cobre outro processo rodando o monitor antigo
# (python -m src.agentes.monitor_pasta) na mesma pasta.
_TRAVA_THREAD = threading.Lock()
CAMINHO_TRAVA_ARQUIVO = "processamento_entrada"


class ProcessamentoEmAndamento(Exception):
    """Outra pessoa (ou outro processo) já está processando a entrada."""


def processar_entrada(acionado_por: str) -> list[dict]:
    modo = config.PROCESSADOR
    if modo == "azure":
        raise NotImplementedError(
            "Processamento no Azure ainda não disponível - depende da entrada no SharePoint. "
            "Use PROCESSADOR=local."
        )
    if modo != "local":
        raise ValueError(f"PROCESSADOR inválido: {modo!r} (use 'local' ou 'azure').")

    if not _TRAVA_THREAD.acquire(blocking=False):
        raise ProcessamentoEmAndamento()
    try:
        with FileLock(config.caminho_trava(CAMINHO_TRAVA_ARQUIVO), timeout=0):
            logger.info("Processamento da entrada acionado por %s.", acionado_por)
            resultados = monitor_pasta.processar_entrada()
            logger.info("Processamento da entrada concluído: %s arquivo(s).", len(resultados))
            return resultados
    except Timeout:
        raise ProcessamentoEmAndamento() from None
    finally:
        _TRAVA_THREAD.release()
