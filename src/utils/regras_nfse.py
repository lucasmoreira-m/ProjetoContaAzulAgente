"""
Regras de negócio FIXAS para lançamento de NFS-e (Nota Fiscal de Serviço
Eletrônica) em contas a pagar - "Novo registro > Compra e Serviço" na
Conta Azul (ver agente_nfs_e.py, que consome estas regras).

REGRA DE BANCO/VENCIMENTO (validada contra o histórico real de produção,
2026-09-08 - varredura read-only via API, 365 dias, ~130 fornecedores de
NFS-e, excluindo lançamentos de retenção de ISS/IRRF e reembolsos de
funcionário que só citam uma NFS-e, confirmada com o usuário):

1. Nota TEM boleto anexado -> paga pela conta BANCO ALFA, vencimento = data do
   boleto (arbitrária, definida pelo fornecedor - por isso o histórico
   real mostra uma distribuição bem espalhada de dias entre competência e
   vencimento nos lançamentos pagos por essa conta).
2. Nota NÃO tem boleto:
   - Fornecedor já tem conta Banco Alfa conhecida (aprendida - ver
     fornecedores_registry.banco_pagamento_sem_boleto) -> paga pela conta
     BANCO ALFA mesmo assim, vencimento no 5º dia útil da competência.
   - Fornecedor sem conta Banco Alfa conhecida -> paga pela conta BANCO BETA,
     vencimento no 5º dia útil da competência.
   (O histórico real confirma isso: lançamentos pagos pela conta Beta
   têm vencimento concentrado em 3-9 dias após a competência, compatível
   com uma regra fixa de "5º dia útil", nunca com uma data arbitrária de
   documento.)

O "banco conhecido" do fornecedor NÃO vem de nenhum cadastro pronto - é
aprendido pelo mesmo Funil de 3 Camadas já usado para categoria/contato em
agente_integrado.py (ver agente_nfs_e.resolver_banco_e_vencimento).
"""

from __future__ import annotations

import unicodedata
from datetime import date, timedelta

from src.core import config
from src.utils import formato_texto

CONTA_FINANCEIRA_ALFA_ID = config.CONTA_FINANCEIRA_ID_PADRAO
CONTA_FINANCEIRA_BETA_ID = config.CONTA_FINANCEIRA_ID_BETA

# Reconfirmação periódica do banco aprendido (2026-09-08, achado em revisão
# pós-implementação): diferente da categoria/contato (que exigem maturação
# ANTES da primeira autonomia total), o banco de um fornecedor sem boleto,
# uma vez aprendido (Camada 1), valia para sempre sem nenhuma checagem nova
# - se o fornecedor trocasse de conta bancária, o sistema continuaria
# pagando pela conta antiga indefinidamente até alguém notar manualmente.
# Depois de N lançamentos automáticos seguidos usando o mesmo banco
# aprendido, sem nenhuma reconfirmação humana no meio, a próxima nota volta
# para revisão manual (sugerindo o mesmo banco, só pedindo 1 clique de
# confirmação) - isso RENOVA a confiança por mais N usos (ver
# fornecedores_registry.registrar_banco_pagamento_sem_boleto, que zera o
# contador a cada confirmação, humana ou por nova inferência do ERP).
LIMITE_USOS_AUTONOMOS_BANCO_SEM_RECONFIRMAR = 10

# Feriados nacionais de DATA FIXA (mês, dia) - suficiente para o horizonte
# curto (5 dias úteis) que esta regra calcula. LIMITAÇÃO CONHECIDA: feriados
# móveis (Carnaval, Sexta-feira Santa, Corpus Christi) NÃO entram nesta
# tabela - se isso se mostrar relevante na prática (nota emitida às
# vésperas de um feriado móvel), o próximo passo natural é trocar por um
# pacote como `holidays`, hoje fora das dependências pinadas do projeto.
_FERIADOS_NACIONAIS_FIXOS = {
    (1, 1),    # Confraternização Universal
    (4, 21),   # Tiradentes
    (5, 1),    # Dia do Trabalho
    (9, 7),    # Independência
    (10, 12),  # Nossa Senhora Aparecida
    (11, 2),   # Finados
    (11, 15),  # Proclamação da República
    (12, 25),  # Natal
}


def _eh_dia_util(d: date) -> bool:
    return d.weekday() < 5 and (d.month, d.day) not in _FERIADOS_NACIONAIS_FIXOS


def calcular_vencimento_dia_util(data_competencia: date, n: int = 5) -> date:
    """Soma N dias úteis a partir de `data_competencia` (pula fins de
    semana e feriados nacionais de data fixa - ver limitação conhecida na
    docstring do módulo). Usado quando a nota não tem boleto (vencimento
    não vem do documento, é uma regra interna fixa da empresa)."""
    d = data_competencia
    dias_uteis_somados = 0
    while dias_uteis_somados < n:
        d += timedelta(days=1)
        if _eh_dia_util(d):
            dias_uteis_somados += 1
    return d


_PALAVRAS_CHAVE_SERVICO_INTERNET = ("internet", "link dedicado")


def _normalizar_texto_servico(texto: str | None) -> str:
    if not texto:
        return ""
    texto = unicodedata.normalize("NFKD", texto).encode("ASCII", "ignore").decode("utf-8")
    return texto.lower()


def eh_servico_internet(*textos: str | None) -> bool:
    """True quando QUALQUER um dos textos passados menciona "internet" ou
    "link dedicado" (sem diferenciar maiúsculas/acentos). Público e
    genérico de propósito (2026-09-15, achado ao conferir ao vivo na Conta
    Azul) - usado tanto pelo fluxo de NFS-e (tipo_servico/
    discriminacao_servicos) quanto pelo de documento comum
    (categoria_sugerida/observações - ver
    agente_integrado.descricao_significativa_do_documento), já que boletos
    de internet real (ex: Link Exemplo Tecnologia/Link Exemplo Telecom) não têm
    "Código do Serviço" e por isso NUNCA validam como NFS-e - caem sempre
    em documento comum, não no fluxo de NFS-e."""
    texto_busca = " ".join(_normalizar_texto_servico(t) for t in textos)
    return any(palavra in texto_busca for palavra in _PALAVRAS_CHAVE_SERVICO_INTERNET)


def periodo_mes_anterior(data_vencimento) -> str | None:
    """MM/AAAA do mês ANTERIOR ao vencimento, ou None se `data_vencimento`
    não for reconhecível (ver formato_texto.parsear_data).

    CONFERIDO AO TELECOM EXEMPLO NA CONTA AZUL (2026-09-15): os lançamentos reais de
    internet/link dedicado (fornecedores "Link Exemplo Tech" e "Link Exemplo Telecom
    Ltda", categoria "4.07.005. Internet", contrato de 36 parcelas) usam
    SEMPRE o mês anterior ao vencimento como "Período" na descrição (ex:
    vencimento 08/09/2026 -> "Período 08/2026"; vencimento 05/10/2026 ->
    "Período 09/2026") - nunca o mês do próprio vencimento, e nunca uma
    competência extraída separadamente do documento (esses boletos nem tem
    esse conceito claro). Por isso `montar_descricao_servico_internet`
    calcula o período a partir do vencimento, em vez de depender de um
    campo de competência que nem sempre existe (documento comum não tem
    esse campo no schema)."""
    data = formato_texto.parsear_data(data_vencimento)
    if not data:
        return None
    ano, mes = data.year, data.month
    if mes == 1:
        ano, mes = ano - 1, 12
    else:
        mes -= 1
    return f"{mes:02d}/{ano:04d}"


def montar_descricao_servico_internet(numero: str | None, data_vencimento=None) -> str:
    """"FT -{numero}/LinkDedicado - Período {MM/AAAA}" - formato usado há
    meses no histórico real de lançamentos de internet/link dedicado
    (conferido ao vivo na Conta Azul, 2026-09-15; ver periodo_mes_anterior
    para a origem do período). "?" no lugar do período quando
    `data_vencimento` não for reconhecível, em vez de travar o lançamento
    só por causa do texto da descrição."""
    return f"FT -{numero}/LinkDedicado - Período {periodo_mes_anterior(data_vencimento) or '?'}"


def montar_descricao_nfse(
    numero_nota: str,
    tipo_servico: str | None,
    discriminacao_servicos: str | None = None,
    data_vencimento=None,
) -> str:
    """"campo descrição deve ter = NFS-e + número da nota + tipo de
    serviço" (regra literal pedida pelo usuário) - EXCETO para serviço de
    internet/link dedicado (2026-09-15, pedido explícito do usuário: mesmo
    padrão de descrição já usado há meses no histórico real de produção -
    ver montar_descricao_servico_internet), que usa "FT -{numero}/
    LinkDedicado - Período {MM/AAAA}" em vez do formato padrão. Gatilho por
    PALAVRA-CHAVE (não por fornecedor/CNPJ específico - decisão explícita
    do usuário): vale para "internet"/"link dedicado" em `tipo_servico` OU
    `discriminacao_servicos`, de QUALQUER fornecedor.

    `discriminacao_servicos` e `data_vencimento` são opcionais só para não
    quebrar quem já chamava esta função com só os 2 primeiros argumentos -
    sem `discriminacao_servicos` a detecção usa só `tipo_servico`."""
    if eh_servico_internet(tipo_servico, discriminacao_servicos):
        return montar_descricao_servico_internet(numero_nota, data_vencimento)
    if tipo_servico:
        return f"NFS-e {numero_nota} - {tipo_servico}"
    return f"NFS-e {numero_nota}"


def montar_observacao_nfse(
    discriminacao_servicos: str,
    codigo_servico: str,
    descricao_codigo_servico: str | None,
) -> str:
    """A API de criação de lançamento da Conta Azul não tem campos
    estruturados para "código do serviço" nem "discriminação dos
    serviços" (só descricao/observacao livres) - por isso ambos vão para
    o campo observação, em texto claro e rotulado."""
    linhas = [f"Discriminação dos serviços: {discriminacao_servicos}"]
    if descricao_codigo_servico:
        linhas.append(f"Código do serviço: {codigo_servico} - {descricao_codigo_servico}")
    else:
        linhas.append(f"Código do serviço: {codigo_servico}")
    return "\n".join(linhas)
