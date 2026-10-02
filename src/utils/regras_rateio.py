"""
Tabela de regras de rateio da empresa.

Cada entrada mapeia um fornecedor (nome normalizado, sem acento e minúsculo)
para uma lista de centros de custo e o percentual que cada um deve receber.
Os percentuais de cada fornecedor devem somar exatamente 100.

Esta tabela é o "conhecimento de negócio" da empresa - não é algo que a IA
deveria adivinhar a cada documento. Quando a empresa tiver uma nova regra
de rateio (novo fornecedor, ou mudança de percentual), é aqui que se ajusta,
sem precisar mexer no código do agente.

CORREÇÃO (ver relatório de auditoria, escopo 5 - qualidade/consistência de
dados): antes, se alguém editasse esta tabela e errasse a soma dos
percentuais, o erro só seria percebido em produção, comparando o rateio
calculado com o valor total do documento. Agora, `validar_integridade()`
roda automaticamente na importação do módulo e falha alto e claro se algum
fornecedor não somar 100%.
"""

from __future__ import annotations

import unicodedata

REGRAS_RATEIO: dict[str, list[dict]] = {
    # Vazio de propósito: a regra "imobiliaria central ltda" que existia
    # aqui era dado de exemplo/teste (confirmado com o usuário em
    # 2026-08-27), nunca foi uma regra de negócio real da empresa - removida
    # ao migrar para a conta real, em vez de apontar para UUIDs
    # inventados. Adicione aqui as regras de rateio fixo que a Empresa de
    # fato usa, quando forem definidas - use o mesmo formato:
    #
    # "nome do fornecedor (minúsculo, sem acento)": [
    #     {"centro_custo_id": "UUID_REAL_DA_CONTA_AZUL", "centro_custo_nome": "...", "percentual": XX},
    #     ...
    # ],
    #
    # Os percentuais de cada fornecedor devem somar exatamente 100 -
    # validar_integridade() abaixo confere isso automaticamente.
}

_TOLERANCIA_PERCENTUAL = 0.01


def validar_integridade() -> None:
    for fornecedor, regra in REGRAS_RATEIO.items():
        soma = sum(item["percentual"] for item in regra)
        if abs(soma - 100) > _TOLERANCIA_PERCENTUAL:
            raise ValueError(
                f"REGRAS_RATEIO['{fornecedor}'] soma {soma}%, deveria somar 100%. "
                f"Corrija regras_rateio.py antes de usar esta regra em produção."
            )


validar_integridade()


def normalizar(texto: str) -> str:
    texto = unicodedata.normalize("NFKD", texto).encode("ASCII", "ignore").decode("utf-8")
    return texto.lower().strip().rstrip(".")


def buscar_regra_rateio(nome_fornecedor: str) -> list[dict] | None:
    """Busca exata primeiro (mais segura); cai para correspondência parcial
    só se necessário, e só entre nomes com mais de 5 caracteres dos dois
    lados - evita falso-positivo de nome curto batendo por substring com
    um fornecedor sem relação nenhuma (ex: "SP" dentro de "Espaço")."""
    nome_norm = normalizar(nome_fornecedor)

    if nome_norm in REGRAS_RATEIO:
        return REGRAS_RATEIO[nome_norm]

    for chave, regra in REGRAS_RATEIO.items():
        chave_norm = normalizar(chave)
        if len(nome_norm) > 5 and len(chave_norm) > 5 and (chave_norm in nome_norm or nome_norm in chave_norm):
            return regra

    return None


def calcular_rateio(valor_total: float, regra: list[dict]) -> list[dict]:
    """
    Calcula o valor de cada centro de custo a partir dos percentuais da regra.
    Ajusta o último item para absorver eventuais centavos de arredondamento,
    garantindo que a soma bata exatamente com o valor_total.
    """
    splits = []
    soma_parcial = 0.0

    for i, item in enumerate(regra):
        if i == len(regra) - 1:
            valor_item = round(valor_total - soma_parcial, 2)
        else:
            valor_item = round(valor_total * (item["percentual"] / 100), 2)
            soma_parcial += valor_item

        splits.append({
            "centro_custo_id": item["centro_custo_id"],
            "centro_custo_nome": item["centro_custo_nome"],
            "percentual": item["percentual"],
            "valor": valor_item,
        })

    return splits
