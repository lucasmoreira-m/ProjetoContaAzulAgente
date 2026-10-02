"""
Backup automático com rotação para os registros locais de aprendizado
(fornecedores_registry.json, historico_centros_funcionarios.json).

POR QUE ISSO EXISTE (2026-09-04, pedido explícito do usuário):
Esses dois arquivos guardam TODO o aprendizado acumulado do agente (CNPJs
confirmados, Contador de Maturação, Diretrizes Globais de Texto) e ficam de
propósito fora do controle de versão (.gitignore - são dado de runtime, não
código). Sem nenhuma cópia de segurança em outro lugar, apagar um desses
arquivos por engano (limpeza de pasta, reinstalação, disco corrompido, ou
substituir por engano o arquivo de quem está testando ao mandar uma
atualização do antigo .exe)
apaga meses de aprendizado sem nenhuma chance de recuperar.

COMO FUNCIONA:
Chamado no FINAL de cada escrita bem-sucedida (depois do `os.replace`
atômico já ter trocado o arquivo principal - o dado real já está salvo com
segurança antes desta função rodar; o backup é só uma cópia extra, nunca o
caminho crítico). Salva uma cópia com timestamp dentro de uma subpasta
"backups/<nome_do_arquivo_sem_extensao>/", e apaga as cópias mais antigas
além do limite (MANTER_ULTIMOS_BACKUPS), para não crescer sem limite com o
tempo (cada gravação bem-sucedida gera um novo backup).

Falha ao fazer backup NUNCA lança exceção nem trava quem chamou - o dado
principal já foi salvo com sucesso antes desta chamada; perder só a cópia
de segurança desta vez não pode impedir o fluxo normal.
"""

from __future__ import annotations

import shutil
from datetime import datetime, timezone
from pathlib import Path

from src.core import config

logger = config.get_logger(__name__)

MANTER_ULTIMOS_BACKUPS = 30


def fazer_backup_com_rotacao(caminho_original: Path, manter_ultimos: int = MANTER_ULTIMOS_BACKUPS) -> None:
    """Copia `caminho_original` para `backups/<nome_sem_extensao>/<nome>_<timestamp><ext>`
    (pasta irmã de `caminho_original`, nunca dentro de entrada/processados/
    revisao/descartados - ver conversa com o usuário sobre isso não ser o
    lugar certo), depois apaga os backups mais antigos além de
    `manter_ultimos`. Nunca lança exceção."""
    try:
        if not caminho_original.exists():
            return

        pasta_backups = caminho_original.parent / "backups" / caminho_original.stem
        pasta_backups.mkdir(parents=True, exist_ok=True)

        timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S_%f")
        destino = pasta_backups / f"{caminho_original.stem}_{timestamp}{caminho_original.suffix}"
        shutil.copy2(caminho_original, destino)

        if manter_ultimos > 0:
            backups_existentes = sorted(
                pasta_backups.glob(f"{caminho_original.stem}_*{caminho_original.suffix}")
            )
            for antigo in backups_existentes[:-manter_ultimos]:
                antigo.unlink(missing_ok=True)
    except Exception:
        logger.exception(
            "Falha ao fazer backup de %s - o dado principal foi salvo normalmente, só esta cópia "
            "extra de segurança falhou.", caminho_original,
        )
