"""
Teto de valor para lançamento automático (2026-09-25).

Documento de fornecedor (NFS-e ou documento comum) acima do teto nunca
lança sozinho - vai para revisão humana com uma mensagem explicando que é
uma MEDIDA DE SEGURANÇA por valor fora do padrão recorrente, não um erro no
documento. Valores dos tetos e o motivo de cada um: ver src/core/config.py.
"""

from __future__ import annotations

import re

from src.core import config

TIPO_NFSE = "nfse"
TIPO_DOCUMENTO_COMUM = "documento_comum"

_NOME_TIPO = {TIPO_NFSE: "NFS-e", TIPO_DOCUMENTO_COMUM: "documento comum (fatura/boleto/NF)"}


def teto_para(tipo: str) -> float:
    return config.TETO_AUTONOMIA_NFSE if tipo == TIPO_NFSE else config.TETO_AUTONOMIA_DOCUMENTO_COMUM


def formatar_reais(valor: float) -> str:
    """1234567.8 -> 'R$ 1.234.567,80'."""
    return "R$ " + f"{valor:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def fornecedor_isento(cnpj: str | None) -> bool:
    """CNPJ na lista de isentos do teto (config.FORNECEDORES_ISENTOS_TETO_AUTONOMIA),
    comparando só os dígitos - aceita com ou sem pontuação."""
    digitos = re.sub(r"\D", "", cnpj or "")
    return bool(digitos) and digitos in config.FORNECEDORES_ISENTOS_TETO_AUTONOMIA


def motivo_acima_do_teto(valor: float | None, tipo: str, cnpj: str | None = None) -> str | None:
    """Mensagem pronta para o painel quando o valor passa do teto de
    autonomia deste tipo de documento; None quando está dentro do teto ou
    quando o fornecedor (pelo CNPJ) é isento do teto."""
    if valor is None or fornecedor_isento(cnpj):
        return None
    teto = teto_para(tipo)
    if valor <= teto:
        return None
    return (
        f"MEDIDA DE SEGURANÇA - VALOR FORA DO PADRÃO RECORRENTE: Este lançamento foi para revisão "
        f"porque o valor ({formatar_reais(valor)}) está acima do limite de {formatar_reais(teto)} para "
        f"lançamento automático de {_NOME_TIPO[tipo]}. Esse limite fica acima dos maiores pagamentos "
        f"recorrentes da empresa, então um valor maior que ele não é recorrente e precisa de "
        f"confirmação humana. Não é erro no documento: confira valor e fornecedor e aprove normalmente."
    )
