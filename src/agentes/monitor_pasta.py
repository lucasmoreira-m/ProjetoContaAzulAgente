"""
Monitora a pasta de entrada e processa documentos automaticamente.

Correção importante (ver relatório de auditoria):
- Antes, se `processar_documento` ou `processar_documento_folha` lançassem
  uma exceção não tratada (o que acontecia SEMPRE, por causa dos bugs de
  assinatura de função corrigidos em agente_integrado.py e agente_folha.py),
  essa exceção subia até o loop principal de `monitorar()`, que não tinha
  proteção nenhuma - o processo inteiro morria e o monitoramento parava
  silenciosamente até alguém notar que a janela tinha fechado.
  Agora, um erro ao processar UM arquivo é logado e o loop continua
  monitorando normalmente; o arquivo problemático fica em processados/
  ou revisao/ conforme o resultado, e nunca trava o monitor inteiro.
"""

from __future__ import annotations

import json
import os
import shutil
import time
from datetime import datetime

from src.core import config
from src.utils import historico_lancamentos
from src.utils import verificacao_duplicidade
from src.agentes import maestro

logger = config.get_logger(__name__)

INTERVALO_SEGUNDOS = 15
EXTENSOES_ACEITAS = (".pdf", ".jpg", ".jpeg", ".png")


def processar_documento_com_classificacao(caminho_documento: str, usuario: str | None = None) -> dict:
    """Primeiro checa se este EXATO arquivo já foi processado antes (Camada 1
    de proteção contra duplicata - ver verificacao_duplicidade.py). Se não
    for novidade, delega ao maestro (src/agentes/maestro.py) a decisão de
    qual dos 3 agentes (folha, NFS-e, documento comum) processa o
    documento."""
    registro_anterior = verificacao_duplicidade.verificar_arquivo_ja_processado(caminho_documento)
    if registro_anterior and registro_anterior.get("sucesso"):
        # Só bloqueia quando o resultado anterior foi SUCESSO TOTAL (documento
        # inteiro já lançado). Em caso de falha/sucesso PARCIAL anterior
        # (ex: folha com 4 de 5 tabelas lançadas), deixamos reprocessar - a
        # Camada 2 (checagem ao vivo na Conta Azul, por lançamento individual)
        # é quem evita duplicar especificamente o que já foi lançado, sem
        # bloquear o que ainda precisa ser corrigido/relançado.
        logger.warning(
            "Arquivo idêntico (mesmo conteúdo) já foi processado antes em %s - "
            "protocolo=%s, sucesso=%s. Não reprocessando automaticamente.",
            registro_anterior.get("processado_em"), registro_anterior.get("protocolo"),
            registro_anterior.get("sucesso"),
        )
        return {
            "sucesso": False,
            "motivo": (
                f"POSSÍVEL DUPLICATA: um arquivo com conteúdo IDÊNTICO a este já foi processado em "
                f"{registro_anterior.get('processado_em')} (resultado na época: "
                f"{'lançado, protocolo ' + str(registro_anterior.get('protocolo')) if registro_anterior.get('sucesso') else registro_anterior.get('motivo')}). "
                f"Não reprocessado automaticamente - confirme manualmente antes de aprovar, se for o caso."
            ),
            "detalhes": {"registro_anterior": registro_anterior},
            "_pular_registro_hash": True,  # não sobrescreve o registro original com este aviso
        }

    return maestro.classificar_e_processar_documento(caminho_documento, usuario)


def listar_arquivos_com_usuario() -> list[tuple[str, str]]:
    """Varre a pasta entrada/ procurando arquivos tanto direto na raiz quanto
    dentro de subpastas por pessoa (ex: entrada/Ana/fatura.pdf)."""
    encontrados = []

    for item in os.listdir(config.PASTA_ENTRADA):
        caminho_item = os.path.join(config.PASTA_ENTRADA, item)

        if os.path.isdir(caminho_item):
            usuario = item
            for nome_arquivo in os.listdir(caminho_item):
                if nome_arquivo.lower().endswith(EXTENSOES_ACEITAS):
                    encontrados.append((os.path.join(caminho_item, nome_arquivo), usuario))
        elif item.lower().endswith(EXTENSOES_ACEITAS):
            encontrados.append((caminho_item, "geral"))

    return encontrados


def mover_com_relatorio(caminho_documento: str, pasta_destino: str, resultado: dict, usuario: str = "geral") -> None:
    """Move o documento para a pasta de destino e grava um relatório em
    texto. O _dados.json (formato estruturado que o painel de revisão lê)
    só é gravado quando o destino é PASTA_REVISAO - em processados/, o
    documento já foi lançado com sucesso e ninguém mais lê esse JSON depois
    (nem o painel, que só olha revisao/), então mantemos processados/ com
    só PDF + relatório em texto, sem arquivo redundante."""
    nome_arquivo = os.path.basename(caminho_documento)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

    destino_documento = os.path.join(pasta_destino, f"{timestamp}_{nome_arquivo}")
    shutil.move(caminho_documento, destino_documento)

    nome_base, _ = os.path.splitext(destino_documento)
    caminho_relatorio = f"{nome_base}_relatorio.txt"

    with open(caminho_relatorio, "w", encoding="utf-8") as f:
        f.write(f"Processado em: {datetime.now().strftime('%d/%m/%Y %H:%M:%S')}\n")
        f.write(f"Enviado por: {usuario}\n")
        # CORREÇÃO (2026-09-03, falso positivo real de produção): o texto
        # de "não sucesso" cobre tanto falha real quanto tabela(s) ignorada(s)
        # de propósito sem nenhuma relevante (ver sucesso_geral em
        # processar_documento_com_classificacao) - deixa explícito que
        # NADA foi enviado ao ERP nos dois casos, em vez do texto genérico
        # "REVISÃO NECESSÁRIA" que não deixava isso claro.
        f.write(f"Resultado: {'SUCESSO - Lançado na Conta Azul' if resultado['sucesso'] else '🔴 IGNORADO / REVISÃO - Não enviado para o ERP'}\n")
        f.write(f"Motivo/Detalhe: {resultado['motivo']}\n\n")
        f.write("Dados extraídos:\n")
        f.write(json.dumps(resultado["detalhes"], indent=2, ensure_ascii=False))

    logger.info("Arquivo movido para: %s", destino_documento)
    logger.info("Relatório salvo em: %s", caminho_relatorio)

    if pasta_destino == config.PASTA_PROCESSADOS:
        if resultado["sucesso"]:
            historico_lancamentos.registrar_lancamentos_de_resultado(destino_documento, "automatico", resultado["detalhes"])
        return

    caminho_json = f"{nome_base}_dados.json"
    with open(caminho_json, "w", encoding="utf-8") as f:
        json.dump({
            "documento_original": os.path.basename(destino_documento),
            "enviado_por": usuario,
            "processado_em": datetime.now().strftime("%d/%m/%Y %H:%M:%S"),
            "sucesso": resultado["sucesso"],
            "motivo": resultado["motivo"],
            "cadastros_pendentes": resultado.get("cadastros_pendentes", []),
            "dados": resultado["detalhes"],
        }, f, indent=2, ensure_ascii=False)


def _garantir_pastas() -> None:
    for pasta in (config.PASTA_ENTRADA, config.PASTA_PROCESSADOS, config.PASTA_REVISAO, config.PASTA_DESCARTADOS):
        os.makedirs(pasta, exist_ok=True)


def processar_arquivo(caminho_completo: str, usuario: str) -> dict:
    """Processa UM documento da entrada do começo ao fim: extrai/lança,
    registra o hash e move para processados/ ou revisao/. Devolve um resumo
    para a tela (arquivo, quem enviou, sucesso, motivo, destino)."""
    logger.info("Documento enviado por: %s", usuario)
    try:
        resultado = processar_documento_com_classificacao(caminho_completo, usuario)
    except Exception:
        # Nenhum erro individual pode derrubar o lote inteiro.
        logger.exception("Erro inesperado ao processar %s - movendo para revisão.", caminho_completo)
        resultado = {
            "sucesso": False,
            "motivo": "Erro inesperado durante o processamento. Veja os logs para detalhes.",
            "detalhes": {},
            "_pular_registro_hash": True,  # falha técnica não deve bloquear retry futuro
        }

    # Registra o hash do conteúdo ANTES de mover (o caminho original
    # só existe até aqui) - protege contra reprocessar o mesmo
    # arquivo de novo no futuro. Falhas técnicas sem dado real
    # extraído (detalhes vazio) NÃO são registradas, para permitir
    # nova tentativa depois que o problema técnico (ex: cota do
    # Gemini, rede) for resolvido - só bloqueamos reprocessamento
    # quando já sabemos de verdade o que esse documento é.
    if not resultado.get("_pular_registro_hash") and resultado.get("detalhes"):
        try:
            verificacao_duplicidade.registrar_arquivo_processado(caminho_completo, resultado)
        except Exception:
            logger.exception("Falha ao registrar hash de %s para proteção contra duplicata.", caminho_completo)

    pasta_destino = config.PASTA_PROCESSADOS if resultado["sucesso"] else config.PASTA_REVISAO
    try:
        mover_com_relatorio(caminho_completo, pasta_destino, resultado, usuario)
    except Exception:
        logger.exception("Falha ao mover/gravar relatório para %s", caminho_completo)

    return {
        "arquivo": os.path.basename(caminho_completo),
        "usuario": usuario,
        "sucesso": bool(resultado["sucesso"]),
        "motivo": resultado.get("motivo"),
        "destino": "processados" if resultado["sucesso"] else "revisao",
    }


def processar_entrada() -> list[dict]:
    """Uma passada pela pasta entrada/: processa o que estiver lá AGORA e
    para (botão "Processar entrada" do painel web, 2026-09-30). Arquivos que
    chegarem durante o processamento ficam para a próxima passada."""
    _garantir_pastas()
    return [processar_arquivo(caminho, usuario) for caminho, usuario in listar_arquivos_com_usuario()]


def monitorar() -> None:
    _garantir_pastas()
    ambiente = config.conta_azul_ambiente()
    if ambiente == "producao":
        logger.warning("=" * 60)
        logger.warning("🔴 AMBIENTE: PRODUÇÃO REAL - lançamentos afetam a Conta Azul da empresa de verdade.")
        logger.warning("=" * 60)
    else:
        logger.info("=" * 60)
        logger.info("🧪 AMBIENTE: TESTE - nenhum lançamento aqui afeta a Conta Azul real.")
        logger.info("=" * 60)
    logger.info("Monitorando a pasta '%s' a cada %s segundos...", config.PASTA_ENTRADA, INTERVALO_SEGUNDOS)
    logger.info("Tipos de arquivo aceitos: %s", ", ".join(EXTENSOES_ACEITAS))

    while True:
        if not processar_entrada():
            logger.debug("Nenhum arquivo novo na pasta entrada/...")

        time.sleep(INTERVALO_SEGUNDOS)


if __name__ == "__main__":
    config.validar_configuracao()
    monitorar()