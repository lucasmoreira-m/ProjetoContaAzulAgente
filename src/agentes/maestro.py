"""
Maestro: decide qual dos 3 agentes processa cada documento que cai em
entrada/ - folha de pagamento (agente_folha), NFS-e (agente_nfs_e) ou
fatura/prestação de contas comum (agente_integrado). Antes desta função
existir, essa decisão vivia embutida em monitor_pasta.py como uma
heurística de 2 vias só (folha vs. comum) - agora com um terceiro braço.

ORDEM DE TENTATIVA (cada uma cai silenciosamente pra próxima se não bater -
mesmo padrão try/except já usado para a extração de folha):
  1. Folha de pagamento (parece_resumo_de_folha) - como antes.
  2. NFS-e (agente_nfs_e) - só quando a extração NÃO sinalizar que é, na
     verdade, uma prestação de contas/reembolso de colaborador citando uma
     NFS-e (ver agente_nfs_e.parece_nfse_direta) - caso real encontrado na
     varredura do histórico de produção (2026-09-08): uma nota de serviço
     pode aparecer dentro de um reembolso, e esse caso precisa continuar
     no fluxo de autonomia zero do agente_integrado, nunca neste agente
     novo.
  3. Documento comum/fatura (agente_integrado) - fallback de sempre.
"""

from __future__ import annotations

from src.agentes import agente_nfs_e
from src.agentes.agente_folha import extrair_dados_folha, processar_documento_folha
from src.agentes.agente_integrado import processar_documento
from src.core import config
from src.utils import classificacao_documento
from src.utils.regras_folha_pagamento import REGRAS_FOLHA

logger = config.get_logger(__name__)


def parece_resumo_de_folha(dados_folha: dict) -> bool:
    """Considera documento de folha quando a extração encontrou pelo menos
    uma tabela com "tipo" preenchido (não-nulo) E (cujo tipo já tem regra
    cadastrada em REGRAS_FOLHA OU tem rateio) - checar só "tipo in
    REGRAS_FOLHA" já causou uma classificação errada real em produção
    (documento de folha de tipo novo, sem regra cadastrada ainda).

    CORREÇÃO (2026-09-14, caso real de produção - boleto do fornecedor
    "Link Exemplo Tecnologia" classificado como folha): exigir "tipo" truthy
    é o que faltava. O prompt de extração (extrair_resumo_folha.py) é
    explícito - "tipo": null significa "o documento genuinamente NÃO
    parece ter relação com folha de pagamento" - mas antes desta função
    ignorava esse sinal e confiava só em "rateio" estar preenchido. Só
    que a MESMA extração é instruída a vasculhar QUALQUER canto do
    documento atrás de anotações como "C. RESP"/"CENTRO"/"CUSTO" e nunca
    devolver "rateio": [] quando achar um valor - e um boleto comum pode
    ter uma anotação bancária de centro de custo interno (ex: "Centro
    Custo/Resultado: 1126" no recibo do Banco Alfa) que não tem NADA a ver com
    folha. Isso produzia "tipo": null com "rateio" preenchido mesmo
    assim, e a checagem antiga classificava como folha só pelo rateio -
    o documento nunca chegava em agente_integrado.processar_documento
    (onde o fornecedor É buscado por CNPJ/nome), então "não achava o
    fornecedor": o agente de folha nem tenta, ele procura funcionário."""
    tabelas = dados_folha.get("tabelas") or []
    return any(
        tabela.get("tipo") and (tabela.get("tipo") in REGRAS_FOLHA or tabela.get("rateio"))
        for tabela in tabelas
    )


# Tentativas de CLASSIFICAÇÃO (folha? NFS-e?) usam max_tentativas=1 - uma
# falha nelas só faz o maestro cair pra próxima candidata mais rápido, sem
# esgotar o retry/backoff completo (até 5 tentativas, ~2min de espera) que
# faz sentido para a extração "de verdade" (a única que efetivamente
# processa o documento). Achado em revisão pós-implementação: sem isso,
# um documento comum (o caso mais frequente) pagava o custo de retry
# completo de folha E de NFS-e antes de finalmente tentar a extração real,
# até 3x mais caro/lento que antes da NFS-e existir, pior ainda durante
# uma instabilidade real do Gemini.
_MAX_TENTATIVAS_CLASSIFICACAO = 1


def classificar_e_processar_documento(caminho_documento: str, usuario: str | None = None) -> dict:
    """Ponto de entrada único do maestro - substitui o antigo
    monitor_pasta.processar_documento_com_classificacao. Duplicata de
    ARQUIVO idêntico continua sendo checada por quem chama esta função
    (monitor_pasta.monitorar), não aqui."""
    try:
        dados_folha = extrair_dados_folha(caminho_documento, max_tentativas=_MAX_TENTATIVAS_CLASSIFICACAO)
    except Exception as e:
        logger.info("Não foi possível tentar a extração de folha (%s); tentando NFS-e/documento comum.", e)
        dados_folha = None

    if dados_folha and parece_resumo_de_folha(dados_folha):
        logger.info("Documento identificado como FOLHA DE PAGAMENTO: %s", caminho_documento)
        return _processar_folha(dados_folha, usuario, caminho_documento)

    try:
        dados_nfse = agente_nfs_e.extrair_dados_nfse(caminho_documento, max_tentativas=_MAX_TENTATIVAS_CLASSIFICACAO)
    except agente_nfs_e.NFSeExtracaoIncompleta as e:
        # A extração RECONHECEU uma NFS-e (campos de identidade batem),
        # só faltou um campo secundário (ex: boleto sem data) - vai direto
        # para revisão humana, preservando o que já foi extraído. NUNCA
        # cai silenciosamente no fluxo de documento comum (achado em
        # revisão: cair lá classificaria tudo errado sem avisar ninguém).
        # EXCEÇÃO (2026-09-25): se a confirmação de NFS-e diz que não é uma
        # (ver classificacao_documento.py), os "campos de identidade" foram
        # inventados pela IA - segue como documento comum.
        nao_e_nfse = classificacao_documento.motivo_para_nao_ser_nfse(e.dados_brutos, caminho_documento)
        if not nao_e_nfse:
            logger.warning("NFS-e reconhecida, mas extração incompleta (%s) - revisão manual direta.", e)
            dados_incompletos = dict(e.dados_brutos)
            dados_incompletos["tipo_documento"] = "nfse"
            return {
                "sucesso": False,
                "motivo": f"Extração de NFS-e incompleta: {e}",
                "detalhes": dados_incompletos,
                "cadastros_pendentes": [],
            }
        logger.info("Extração de NFS-e incompleta descartada - %s: %s", nao_e_nfse, caminho_documento)
        dados_nfse = None
    except Exception as e:
        logger.info("Não foi possível tentar a extração de NFS-e (%s); tentando documento comum.", e)
        dados_nfse = None

    if dados_nfse and agente_nfs_e.parece_nfse_direta(dados_nfse):
        # Passar na validação do schema não basta (2026-09-25): a IA inventa
        # campos de NFS-e numa fatura comum - ver classificacao_documento.py.
        nao_e_nfse = classificacao_documento.motivo_para_nao_ser_nfse(dados_nfse, caminho_documento)
        if nao_e_nfse:
            logger.info("Extração de NFS-e descartada - %s: %s", nao_e_nfse, caminho_documento)
        else:
            logger.info("Documento identificado como NFS-e: %s", caminho_documento)
            return agente_nfs_e.processar_dados_nfse(dados_nfse, caminho_documento, usuario)

    logger.info("Documento identificado como FATURA/PRESTAÇÃO DE CONTAS comum: %s", caminho_documento)
    return processar_documento(caminho_documento, usuario)


def _processar_folha(dados_folha: dict, usuario: str | None, caminho_documento: str) -> dict:
    """Extraído sem alteração de comportamento do antigo
    monitor_pasta.processar_documento_com_classificacao (mesma agregação
    de sucesso_geral e cadastros_pendentes)."""
    resultados_por_tabela = processar_documento_folha(dados_folha, usuario, caminho_documento)
    # Tabelas ignoradas DE PROPÓSITO (não circuladas, ex: Pró-labore) não
    # contam como falha. Tabelas de tipo desconhecido/sem regra SÃO uma
    # falha real - precisam de revisão humana.
    relevantes = {
        chave: r for chave, r in resultados_por_tabela.items()
        if r["sucesso"] or not r.get("ignorado_propositalmente")
    }
    # Só é sucesso quando existe pelo menos uma tabela relevante E todas
    # elas realmente lançaram - sem tabela relevante nenhuma, cai em
    # revisão humana (ver CHANGELOG.md: correção de falso positivo real).
    sucesso_geral = bool(relevantes) and all(r["sucesso"] for r in relevantes.values())

    cadastros_pendentes_agregados = []
    for r in resultados_por_tabela.values():
        if not r["sucesso"] and not r.get("ignorado_propositalmente"):
            cadastros_pendentes_agregados.extend(r.get("cadastros_pendentes", []))

    return {
        "sucesso": sucesso_geral,
        "motivo": "; ".join(f"{chave.upper()}: {r['motivo']}" for chave, r in resultados_por_tabela.items()),
        "detalhes": resultados_por_tabela,
        "cadastros_pendentes": cadastros_pendentes_agregados,
    }
