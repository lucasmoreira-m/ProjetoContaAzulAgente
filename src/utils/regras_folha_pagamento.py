"""
Regras de negócio para lançamentos derivados da folha de pagamento
(FGTS, INSS, IRRF, Salários, Bolsa de Estágio, Adiantamento Salarial).

Um único documento de "resumo da folha" pode gerar VÁRIOS lançamentos
diferentes na Conta Azul, cada um seguindo uma regra própria de fornecedor,
categoria, data de competência/vencimento e forma de pagamento.

Nenhuma dessas regras deve ser "adivinhada" pela IA a cada documento -
são regras fixas da empresa, mantidas aqui.

IMPORTANTE sobre rateio por centro de custo nesses documentos:
O rateio costuma vir como uma MARCAÇÃO VISUAL dentro do próprio documento
(ex: um print/imagem colado com destaque ou sublinhado em vermelho mostrando
a divisão entre centros de custo) - não como texto corrido comum. O prompt
de extração precisa instruir a IA a procurar especificamente por esse tipo
de destaque visual, além de texto simples, sempre que processar documentos
de folha de pagamento.

Status atual (foco de teste): regra de INSS habilitada e validada com o
campo "Total INSS" do resumo da folha. FGTS e IRRF ficam para depois -
IRRF em especial tem uma ambiguidade não resolvida ainda (o documento traz
dois totais diferentes: "conforme competência do cálculo" e "conforme
competência do pagamento" - falta decidir qual usar antes de habilitar).
"""

import calendar
from datetime import date, timedelta

# Sinaliza, no rateio extraído pela IA para tabelas de ferias/pro_labore, que
# o centro de custo não veio marcado no documento e precisa ser resolvido
# depois (histórico do funcionário, ou escolha manual no painel). Vive aqui
# (módulo sem imports de projeto) para poder ser importado tanto por
# extrair_resumo_folha.py (onde o filtro de linhas de total não pode
# descartar essa linha) quanto por agente_folha.py (onde a cascata de
# resolução é aplicada), sem risco de import circular.
CENTRO_CUSTO_PLACEHOLDER = "A_DEFINIR"

# Regra fixa de negócio (2026-09-02): Termo de Rescisão NUNCA tem uma regra
# própria em REGRAS_FOLHA (cada lançamento é de um funcionário desligado
# diferente - não dá pra cadastrar um "fornecedor_nome" fixo como FGTS/
# INSS/etc.), e o "cérebro de histórico" (buscar_regra_historico_por_tipo,
# em agente_folha.py) nunca acha nada útil pra esse tipo: cada colaborador
# só é demitido uma vez, então o nome nunca se repete entre documentos.
# Por isso a categoria/conta financeira de rescisão são FIXAS aqui,
# aplicadas sempre (não como sugestão dependente de histórico) - validado
# contra os nomes reais cadastrados na Conta Azul.
REGRA_RESCISAO_FIXA = {
    "categoria_nome": "4.02.006. Rescisões - TRCT",
    "conta_financeira_nome": "Banco Alfa - Matriz",
    "forma_pagamento": "Transferência Bancária",
}


def ajustar_para_dia_util_anterior(data):
    """Se a data cair em sábado ou domingo, antecipa para a sexta-feira anterior
    (regra: 'sempre dia útil, ao contrário antecipar')."""
    while data.weekday() >= 5:  # 5 = sábado, 6 = domingo
        data -= timedelta(days=1)
    return data


def ultimo_dia_util_do_mes(ano, mes):
    """Último dia do mês informado, ajustado para o dia útil anterior se cair em fim de semana."""
    ultimo_dia = calendar.monthrange(ano, mes)[1]
    return ajustar_para_dia_util_anterior(date(ano, mes, ultimo_dia))


def dia_20_mes_seguinte(ano, mes):
    """Dia 20 do mês seguinte à competência informada, ajustado para dia útil."""
    if mes == 12:
        ano_seguinte, mes_seguinte = ano + 1, 1
    else:
        ano_seguinte, mes_seguinte = ano, mes + 1
    return ajustar_para_dia_util_anterior(date(ano_seguinte, mes_seguinte, 20))


def dia_15_do_mes(ano, mes):
    """Dia 15 do mês informado, ajustado para dia útil."""
    return ajustar_para_dia_util_anterior(date(ano, mes, 15))


# Tabela de regras por tipo de lançamento derivado da folha.
# "campo_valor" indica de qual campo extraído do documento vem o valor total.
REGRAS_FOLHA = {
    "fgts": {
        "fornecedor_nome": "FGTS",
        "categoria_nome": "4.02.008. FGTS s/ Salário",
        "descricao_template": "FGTS s/Folha de pagamento {competencia}",
        "forma_pagamento": "Boleto bancário",
        "conta_financeira_nome": "Banco Alfa - Matriz",
        "campo_valor": "valor_fgts",
        "data_competencia_func": ultimo_dia_util_do_mes,
        "vencimento_func": dia_20_mes_seguinte,
    },
    "inss": {
        "fornecedor_nome": "Ministério da Fazenda",
        "categoria_nome": "4.02.010. INSS s/ Salário",
        "descricao_template": "INSS/ Folha Competência {competencia}",
        "forma_pagamento": "Boleto bancário",
        "conta_financeira_nome": "Banco Alfa - Matriz",
        "campo_valor": "valor_inss",
        "data_competencia_func": ultimo_dia_util_do_mes,
        "vencimento_func": dia_20_mes_seguinte,
    },
    "irrf_folha": {
        "fornecedor_nome": "Instituto Nacional do Seguro Social",
        "categoria_nome": "4.02.007. IRRF s/ Salário",
        "descricao_template": "IRRF/ Folha Competência {competencia}",
        "forma_pagamento": "Boleto bancário",
        "conta_financeira_nome": "Banco Alfa - Matriz",
        "campo_valor": "valor_irrf_pagamento",  # confirmado: usa o valor conforme competência do pagamento
        "data_competencia_func": ultimo_dia_util_do_mes,
        "vencimento_func": dia_20_mes_seguinte,
    },
    "salarios": {
        "fornecedor_nome": "Empresa Exemplo Ltda",
        "categoria_nome": "4.02.005. Salários",
        "descricao_template": "Salário - Data Folha: {data_pagamento}",
        "forma_pagamento": "Transferência Bancária",
        "conta_financeira_nome": None,  # depende do arquivo bancário (Banco Alfa ou Beta) - ver regra específica
        "campo_valor": "valor_salarios",
        "data_competencia_func": ultimo_dia_util_do_mes,
        "vencimento_func": ultimo_dia_util_do_mes,
    },
    "bolsa_estagio": {
        "fornecedor_nome": "Empresa Exemplo Ltda",
        "categoria_nome": "4.02.011. Bolsa de Estágio",
        "descricao_template": "Bolsa de estágio - Data Folha: {data_pagamento}",
        "forma_pagamento": "Transferência Bancária",
        "conta_financeira_nome": None,
        "campo_valor": "valor_bolsa_estagio",
        "data_competencia_func": ultimo_dia_util_do_mes,
        "vencimento_func": ultimo_dia_util_do_mes,
    },
    "adiantamento_salarial": {
        "fornecedor_nome": "Empresa Exemplo Ltda",
        "categoria_nome": "4.02.003. Adiantamento Salarial",
        "descricao_template": "Adiantamento de salários {competencia}",
        "forma_pagamento": "Transferência Bancária",
        "conta_financeira_nome": None,
        "campo_valor": "valor_adiantamento",
        "data_competencia_func": dia_15_do_mes,
        "vencimento_func": dia_15_do_mes,
    },
    # "ferias" e "pro_labore" são diferentes de todos os outros tipos acima:
    # cada lançamento é de UM funcionário específico (o "fornecedor" real é
    # o nome da pessoa, extraído do próprio documento - não um fornecedor
    # fixo da empresa toda). Por isso "fornecedor_nome" fica None aqui - é
    # um sinal para agente_folha.py usar o campo "funcionario" da tabela em
    # vez desta regra. Confirmado com o histórico real de lançamentos na
    # Conta Azul (2026-08-31): a categoria é sempre a mesma, mas fornecedor
    # e centro de custo variam por funcionário.
    "ferias": {
        "fornecedor_nome": None,  # usar tabela["funcionario"]
        "categoria_nome": "4.02.004. Férias",
        # Padrão contábil validado no ERP (confirmado com o histórico real
        # de lançamentos na Conta Azul): "Férias - " + o período de gozo
        # exatamente como escrito no documento (ex: "Férias - 30/09/2026 A
        # 14/10/2026 = 15 Dias"), não o nome do funcionário. Se o documento
        # não trouxer essa linha (TabelaFolha.periodo_gozo vazio),
        # agente_folha.py usa o nome do funcionário como alternativa, para
        # a descrição nunca ficar em branco.
        "descricao_template": "Férias - {periodo_gozo}",
        "forma_pagamento": "Transferência Bancária",
        "conta_financeira_nome": "Banco Alfa - Matriz",
        "campo_valor": None,
        # Sem fórmula de data - usa a data escrita no próprio documento
        # (TabelaFolha.data_pagamento_documento), diferente dos outros
        # tipos que calculam a partir da competência da empresa toda.
        "data_competencia_func": None,
        "vencimento_func": None,
    },
    "pro_labore": {
        "fornecedor_nome": None,  # usar tabela["funcionario"]
        "categoria_nome": "4.04.002. Pró-labore",
        "descricao_template": "Pró-labore - {funcionario}",
        "forma_pagamento": "Transferência Bancária",
        "conta_financeira_nome": "Banco Alfa - Matriz",
        "campo_valor": None,
        "data_competencia_func": None,
        "vencimento_func": None,
    },
    # GRRF (Guia de Recolhimento Rescisório do FGTS) - igual a ferias/
    # pro_labore, é um documento INDIVIDUAL por funcionário desligado, não
    # uma tabela consolidada da empresa toda. A data de vencimento vem
    # escrita no próprio documento (campo "Pagar este documento até"), não
    # de fórmula - por isso data_competencia_func/vencimento_func são None,
    # roteando para o mesmo caminho de ferias/pro_labore em
    # agente_folha.processar_tabela (usa TabelaFolha.data_pagamento_documento
    # e TabelaFolha.funcionario).
    "grrf": {
        "fornecedor_nome": None,  # usar tabela["funcionario"]
        "categoria_nome": "4.02.009. FGTS s/ Rescisão",
        # Sem o "-" antes do funcionário - validado contra o ERP (2026-09-02).
        "descricao_template": "FGTS Rescisão {funcionario}",
        "forma_pagamento": "Boleto bancário",
        "conta_financeira_nome": "Banco Alfa - Matriz",
        "campo_valor": None,
        "data_competencia_func": None,
        "vencimento_func": None,
    },
}


# Rateio PADRÃO da folha de pagamento, usado quando o documento não traz um
# rateio específico daquela competência. É o mesmo padrão para FGTS, INSS e
# IRRF (só muda o total a dividir) - confirmado pela empresa. A lista de
# centros pode aumentar ou diminuir ao longo do tempo; edite aqui quando isso
# acontecer, sem precisar mexer no código do agente.
RATEIO_PADRAO_FOLHA = [
    {"nome": "1122 - Administrativo", "percentual": 0.7449},
    {"nome": "1124 - Financeiro", "percentual": 1.3283},
    {"nome": "1125 - Gente e Gestão", "percentual": 0.9278},
    {"nome": "1126 - Tecnologia da Informação", "percentual": 1.5428},
    {"nome": "1142 - IM/ Marketing", "percentual": 0.9919},
    {"nome": "2110 - Diretoria Executiva - PMP", "percentual": 0.0},
    {"nome": "2111 - Diretoria de Operações", "percentual": 0.0},
    {"nome": "2114 - Engineering", "percentual": 1.1678},
    {"nome": "2121 - Afastados", "percentual": 0.2054},
    {"nome": "3213 - Itagibá - Atlantic Nickel", "percentual": 4.5493},
    {"nome": "3217 - Desc. Barragens - Vale II", "percentual": 4.7114},
    {"nome": "3223 - Samarco", "percentual": 13.6510},
    {"nome": "3226 - Appian - Atlantic Nickel", "percentual": 4.3842},
    {"nome": "3228 - Cedro Mineração", "percentual": 1.3122},
    {"nome": "3230 - Ferrosos Sudeste - Vale", "percentual": 1.5969},
    {"nome": "3232 - Ferrosos Sul - Vale", "percentual": 4.7442},
    {"nome": "3233 - Cedro - Hospitais", "percentual": 9.6011},
    {"nome": "3255 - Appian - Detronic", "percentual": 3.0930},
    {"nome": "3264 - TIG - Vale", "percentual": 10.0036},
    {"nome": "3265 - Appian - Graphcoa", "percentual": 1.3435},
    {"nome": "3266 - Pelotização - Vale", "percentual": 14.1124},
    {"nome": "3268 - AVG", "percentual": 1.5923},
    {"nome": "3269 - Minas Paralisada - Vale", "percentual": 2.1132},
    {"nome": "3272 - Underground - Atlantic Nickel", "percentual": 11.0241},
    {"nome": "3273 - Allied Graphite - Appian", "percentual": 3.1555},
    {"nome": "3274 - VBM - Vale", "percentual": 1.4401},
    {"nome": "3276 - Projeto Neves - Atlas Lithium", "percentual": 0.6631},
]

# Validação de integridade (Escopo 5 da auditoria): falha alto e claro na
# importação do módulo se alguém editar a tabela e a soma sair de 100%,
# em vez de deixar o erro aparecer só em produção como uma diferença de
# centavos difícil de rastrear.
_soma_percentuais = sum(item["percentual"] for item in RATEIO_PADRAO_FOLHA)
if abs(_soma_percentuais - 100) > 0.05:
    raise ValueError(
        f"RATEIO_PADRAO_FOLHA soma {_soma_percentuais}%, deveria somar 100%. "
        f"Corrija regras_folha_pagamento.py antes de usar em produção."
    )


def calcular_rateio_padrao_folha(buscar_centro_custo_func, access_token, valor_total):
    """Calcula o rateio padrão da folha para um valor_total qualquer (FGTS, INSS, IRRF...).
    Busca o id de cada centro de custo em tempo real na Conta Azul (por nome), e ajusta
    o último item para a soma bater exatamente com o valor_total (Camada 7).
    Recebe a função buscar_centro_custo já pronta (de agente_integrado.py) para não duplicar código."""
    splits = []
    soma_parcial = 0.0
    erros = []

    for i, item in enumerate(RATEIO_PADRAO_FOLHA):
        centro, tipo_match = buscar_centro_custo_func(access_token, item["nome"])
        if not centro or tipo_match != "exato":
            erros.append(f"Centro de custo '{item['nome']}' não encontrado na Conta Azul.")
            continue

        if i == len(RATEIO_PADRAO_FOLHA) - 1:
            valor_item = round(valor_total - soma_parcial, 2)
        else:
            valor_item = round(valor_total * (item["percentual"] / 100), 2)
            soma_parcial += valor_item

        splits.append({
            "centro_custo_id": centro["id"],
            "centro_custo_nome": centro["nome"],
            "percentual": item["percentual"],
            "valor": valor_item
        })

    return splits, erros