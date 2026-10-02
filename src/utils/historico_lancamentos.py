"""
Registro local canônico de lançamentos criados com sucesso na Conta Azul
(automático via monitor_pasta.py, ou manual via painel_revisao.py).

POR QUE ISSO EXISTE:
Antes, ao mover um documento processado para PASTA_PROCESSADOS, só o PDF e um
_relatorio.txt (texto solto, com um JSON de "detalhes" sem IDs de categoria/
contato/centro de custo) ficavam salvos - e no caminho de aprovação MANUAL
pelo painel, nem isso: o _relatorio.txt movido era o ORIGINAL (de quando o
item caiu em revisão), nunca reescrito com o protocolo/dados do lançamento
manual que de fato foi criado.

Isso inviabilizava qualquer feature de "Histórico local com edição": sem os
IDs reais (não só nomes, que exigiriam match aproximado de novo) e sem o
protocolo/id do lançamento manual, não dá para reconstruir um formulário de
edição nem saber o que atualizar na Conta Azul depois.

Este módulo define UM formato canônico (`_lancamento.json`, ao lado do PDF
em PASTA_PROCESSADOS) que os dois caminhos (automático e manual) escrevem,
e que painel_revisao.py lê para montar a tela de Histórico - sem nenhuma
chamada à Conta Azul só para LER a lista (só ao entrar em modo de edição).
"""

from __future__ import annotations

import glob
import json
import os
from datetime import datetime, timezone
from typing import Optional

from src.core import config

logger = config.get_logger(__name__)


def caminho_lancamento_json(caminho_pdf_ou_base: str) -> str:
    """Dado o caminho do PDF já movido para PASTA_PROCESSADOS (ou sua base
    sem extensão), devolve o caminho do _lancamento.json correspondente."""
    base, _ = os.path.splitext(caminho_pdf_ou_base)
    return f"{base}_lancamento.json"


def registrar_lancamento(
    caminho_pdf: str,
    *,
    origem: str,
    tipo: str,
    protocolo: Optional[str],
    descricao: str,
    valor: float,
    data_vencimento: Optional[str],
    data_competencia: Optional[str],
    categoria: Optional[dict],
    contato: Optional[dict],
    rateio: Optional[list[dict]],
    conta_financeira_id: Optional[str] = None,
    forma_pagamento: Optional[str] = None,
    observacoes: Optional[str] = None,
    resposta_criacao: Optional[dict] = None,
    origem_categoria: Optional[str] = None,
    camada_decisao: Optional[str] = None,
    camada2_evidencia_qtd: Optional[int] = None,
    aprovado_por: Optional[str] = None,
) -> None:
    """Grava o registro canônico ao lado do PDF já movido para
    PASTA_PROCESSADOS. `origem`: "automatico" (monitor_pasta.py) ou "manual"
    (aprovado no painel). `categoria`/`contato`: {"id":..., "nome":...}.
    `rateio`: lista de {"centro_custo_id", "centro_custo_nome", "valor"}.

    `origem_categoria`/`camada_decisao`/`camada2_evidencia_qtd` (2026-09-04,
    auditoria do Funil de Decisão Contábil em 3 Camadas - ver
    agente_integrado.py): de onde veio a categoria usada neste lançamento
    ("aprendida", "extraida_novo" ou "inferido_do_erp") e qual camada
    decidiu ("camada1_registro_local", "camada2_erp_inferido" ou
    "camada3_fallback_ia"). Sem isso, um lançamento automático feito pela
    Camada 2 (que lança JÁ NA PRIMEIRA VEZ que vê um fornecedor, sem
    nenhuma aprovação humana anterior - ver CHANGELOG.md) ficaria
    indistinguível de um lançamento de rotina no Histórico local, o que
    esconderia justamente os casos que merecem mais atenção de auditoria."""
    agora = datetime.now(timezone.utc).isoformat()
    registro = {
        "arquivo_original": os.path.basename(caminho_pdf),
        "origem": origem,
        "tipo": tipo,
        "origem_categoria": origem_categoria,
        "camada_decisao": camada_decisao,
        "camada2_evidencia_qtd": camada2_evidencia_qtd,
        # Quem aprovou no painel (2026-09-30, painel web com várias
        # pessoas): None no lançamento automático.
        "aprovado_por": aprovado_por,
        # "id" do lançamento em si (para o PUT de atualização) pode ou não
        # estar em resposta_criacao, dependendo do que a API devolve - ver
        # aviso em agente_integrado.atualizar_lancamento. "protocolo" é o
        # identificador humano que sempre existe, mas é DIFERENTE do "id".
        "protocolo": protocolo,
        "id_lancamento": (resposta_criacao or {}).get("id"),
        "descricao": descricao,
        "observacoes": observacoes,
        "valor": valor,
        "data_vencimento": data_vencimento,
        "data_competencia": data_competencia,
        "categoria": categoria,
        "contato": contato,
        "rateio": rateio or [],
        "conta_financeira_id": conta_financeira_id,
        "forma_pagamento": forma_pagamento,
        "resposta_criacao": resposta_criacao,
        "processado_em": agora,
        "atualizado_em": agora,
    }
    caminho_json = caminho_lancamento_json(caminho_pdf)
    with open(caminho_json, "w", encoding="utf-8") as f:
        json.dump(registro, f, indent=2, ensure_ascii=False)
    logger.info("Registro de histórico local salvo em %s", caminho_json)


def listar_historico() -> list[dict]:
    """Lê todos os _lancamento.json de PASTA_PROCESSADOS - só arquivos
    locais, NENHUMA chamada à Conta Azul. Cada item inclui "_caminho_json"
    (para poder reescrever depois de uma edição bem-sucedida)."""
    caminhos = sorted(
        glob.glob(os.path.join(config.PASTA_PROCESSADOS, "*_lancamento.json")),
        reverse=True,
    )
    itens = []
    for caminho in caminhos:
        try:
            with open(caminho, "r", encoding="utf-8") as f:
                registro = json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            logger.error("Falha ao ler %s (%s) - ignorando este item no histórico.", caminho, e)
            continue
        registro["_caminho_json"] = caminho
        itens.append(registro)
    return itens


def registrar_lancamentos_de_resultado(destino_documento: str, origem: str, detalhes) -> None:
    """Detecta se `detalhes` é o formato de documento comum (um único
    lançamento, dict achatado com "protocolo" direto) ou de folha (dict de
    resultados por tabela, cada um com seu próprio "sucesso"/"detalhes") e
    registra um _lancamento.json para cada lançamento real criado - usado
    tanto pelo caminho automático (monitor_pasta.py) quanto pela aprovação
    manual (painel_revisao.py), para os dois alimentarem o mesmo Histórico
    local com o mesmo formato."""
    if not isinstance(detalhes, dict):
        return

    if "protocolo" in detalhes:
        _registrar_um(destino_documento, origem, "comum", detalhes)
        return

    for chave_tabela, r in detalhes.items():
        if isinstance(r, dict) and r.get("sucesso") and isinstance(r.get("detalhes"), dict):
            _registrar_um(destino_documento, origem, "folha", r["detalhes"], sufixo=chave_tabela)


def _registrar_um(destino_documento: str, origem: str, tipo: str, detalhes: dict, sufixo: Optional[str] = None) -> None:
    caminho_base = destino_documento
    if sufixo:
        nome_base, ext = os.path.splitext(destino_documento)
        caminho_base = f"{nome_base}_{sufixo}{ext}"
    try:
        registrar_lancamento(
            caminho_base,
            origem=origem,
            tipo=tipo,
            protocolo=detalhes.get("protocolo"),
            descricao=detalhes.get("descricao") or "",
            valor=detalhes.get("valor"),
            data_vencimento=detalhes.get("data_vencimento"),
            data_competencia=detalhes.get("data_competencia") or detalhes.get("data_vencimento"),
            categoria=detalhes.get("categoria"),
            contato=detalhes.get("contato"),
            rateio=detalhes.get("rateio_aplicado") or detalhes.get("rateio_calculado"),
            conta_financeira_id=detalhes.get("conta_financeira_id"),
            forma_pagamento=detalhes.get("forma_pagamento"),
            observacoes=detalhes.get("observacoes"),
            resposta_criacao=detalhes.get("resposta_criacao"),
            origem_categoria=detalhes.get("origem_categoria"),
            camada_decisao=detalhes.get("camada_decisao"),
            camada2_evidencia_qtd=detalhes.get("camada2_evidencia_qtd"),
            aprovado_por=detalhes.get("aprovado_por"),
        )
    except Exception:
        # Falha ao salvar o registro de histórico NÃO deve impedir o fluxo
        # normal (documento já foi lançado com sucesso na Conta Azul) - só
        # loga; sem esse registro, o item simplesmente não aparece na tela
        # de Histórico do painel.
        logger.exception("Falha ao registrar histórico local para %s", caminho_base)


def atualizar_apos_edicao(caminho_json: str, registro_atualizado: dict) -> None:
    """Reescreve o _lancamento.json depois de uma edição confirmada na
    Conta Azul (atualiza atualizado_em automaticamente)."""
    registro_atualizado = dict(registro_atualizado)
    registro_atualizado.pop("_caminho_json", None)
    registro_atualizado["atualizado_em"] = datetime.now(timezone.utc).isoformat()
    with open(caminho_json, "w", encoding="utf-8") as f:
        json.dump(registro_atualizado, f, indent=2, ensure_ascii=False)
    logger.info("Histórico local atualizado em %s", caminho_json)
