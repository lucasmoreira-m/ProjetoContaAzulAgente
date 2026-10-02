"""Testes de src/utils/formato_texto.py e dos dois bugs de produção
(2026-09-14) que motivaram o módulo, vistos na tela de detalhe do
lançamento na Conta Azul:

1. Descrição com o literal "None" ("beneficios - None").
2. Data invertida ("2026/09/14" no lugar de "14/09/2026", e o vencimento
   indo invertido para a API).
"""

from datetime import date, datetime

import pytest

from src.agentes import agente_folha, agente_integrado
from src.utils.formato_texto import (
    competencia_para_texto_br, data_para_iso, data_para_texto_br,
    parsear_competencia, parsear_data, texto_ou,
)


# --- texto_ou --------------------------------------------------------------------

@pytest.mark.parametrize("valor", [None, "", "   "])
def test_texto_ou_devolve_padrao_para_vazios(valor):
    assert texto_ou(valor, "padrao") == "padrao"


def test_texto_ou_padrao_e_string_vazia_por_default():
    assert texto_ou(None) == ""


def test_texto_ou_remove_espacos_das_pontas():
    assert texto_ou("  Ticket Solucoes  ") == "Ticket Solucoes"


def test_texto_ou_nunca_devolve_a_string_none():
    # O bug 1 em uma linha: sem isto, a f-string imprime "None".
    assert "None" not in f"beneficios - {texto_ou(None, 'sem numero')}"


# --- parsear_data ----------------------------------------------------------------

@pytest.mark.parametrize("valor", [
    "14/09/2026",   # o formato pedido no prompt
    "2026-09-14",   # o formato da API da Conta Azul
    "2026/09/14",   # o que a IA às vezes devolve
    "14-09-2026",
    "14.09.2026",
])
def test_parsear_data_aceita_as_duas_ordens(valor):
    assert parsear_data(valor) == date(2026, 9, 14)


def test_parsear_data_aceita_date_e_datetime():
    assert parsear_data(date(2026, 9, 14)) == date(2026, 9, 14)
    assert parsear_data(datetime(2026, 9, 14, 10, 30)) == date(2026, 9, 14)


@pytest.mark.parametrize("valor", [
    None, "", "sem data", "31/02/2026", "14/09", "1/2/3", "2026/2026/14",
])
def test_parsear_data_devolve_none_no_que_nao_da_para_confiar(valor):
    assert parsear_data(valor) is None


def test_parsear_data_nao_chuta_dia_versus_mes_ambiguo():
    # 09/10/2026 é legítimo em DD/MM: 9 de outubro. O importante é que o
    # ano continue sendo o extremo de 4 dígitos, sem inverter dia e mês.
    assert parsear_data("09/10/2026") == date(2026, 10, 9)


# --- data_para_texto_br ----------------------------------------------------------

def test_data_para_texto_br_corrige_a_data_invertida():
    # O bug 2 exatamente como apareceu em produção.
    assert data_para_texto_br("2026/09/14") == "14/09/2026"


def test_data_para_texto_br_mantem_o_que_ja_estava_certo():
    assert data_para_texto_br("14/09/2026") == "14/09/2026"


def test_data_para_texto_br_formata_objeto_date():
    assert data_para_texto_br(date(2026, 9, 14)) == "14/09/2026"


def test_data_para_texto_br_preserva_texto_ilegivel_em_vez_de_apagar():
    assert data_para_texto_br("a combinar") == "a combinar"


def test_data_para_texto_br_nao_imprime_none():
    assert data_para_texto_br(None) == ""


# --- parsear_competencia / competencia_para_texto_br -----------------------------

@pytest.mark.parametrize("valor", ["09/2026", "2026/09", "09-2026"])
def test_parsear_competencia_aceita_as_duas_ordens(valor):
    assert parsear_competencia(valor) == (2026, 9)


@pytest.mark.parametrize("valor", [None, "", "13/2026", "sem competencia", "09/10"])
def test_parsear_competencia_devolve_none_no_ambiguo_ou_invalido(valor):
    assert parsear_competencia(valor) is None


def test_competencia_para_texto_br_normaliza_para_mm_aaaa():
    assert competencia_para_texto_br("2026/09") == "09/2026"
    assert competencia_para_texto_br("9/2026") == "09/2026"


def test_competencia_para_texto_br_nao_imprime_none():
    assert competencia_para_texto_br(None) == ""


# --- data_para_iso ---------------------------------------------------------------

def test_data_para_iso_converte_do_formato_br():
    assert data_para_iso("14/09/2026") == "2026-09-14"


def test_data_para_iso_e_idempotente_no_formato_da_api():
    assert data_para_iso("2026-09-14") == "2026-09-14"


def test_data_para_iso_devolve_padrao_em_vez_de_lixo_para_a_api():
    # Ao contrário de data_para_texto_br, aqui NÃO se devolve o texto
    # original: ele iria direto para a API da Conta Azul.
    assert data_para_iso("a combinar") is None
    assert data_para_iso("a combinar", "") == ""


# --- regressão: descrição "beneficios - None" ------------------------------------

def test_descricao_documento_nao_vira_none_quando_falta_numero():
    # Caso real: numero_documento existe no dict com valor None (o schema
    # declara Optional[str] = None), então o default do .get nunca entrava.
    descricao = agente_integrado.descricao_significativa_do_documento(
        {"tipo_documento": "beneficios", "numero_documento": None, "descricao": None}
    )
    assert descricao == "beneficios - sem numero"
    assert "None" not in descricao


def test_descricao_documento_usa_a_descricao_pronta_quando_existe():
    descricao = agente_integrado.descricao_significativa_do_documento(
        {"tipo_documento": "conta_fixa", "numero_documento": "123", "descricao": "Energia 09/2026"}
    )
    assert descricao == "Energia 09/2026"


def test_descricao_documento_ignora_descricao_so_de_espacos():
    descricao = agente_integrado.descricao_significativa_do_documento(
        {"tipo_documento": "conta_fixa", "numero_documento": "123", "descricao": "   "}
    )
    assert descricao == "conta_fixa - 123"


def test_descricao_folha_sem_regra_nao_vira_none():
    # O print de produção: tipo "beneficios" (não está em REGRAS_FOLHA) e
    # competência None -> saía "beneficios - None".
    descricao = agente_folha._descricao_tipo_sem_regra("beneficios", None, None)
    assert descricao == "beneficios"
    assert "None" not in descricao


def test_descricao_folha_sem_regra_prefere_funcionario():
    assert agente_folha._descricao_tipo_sem_regra(
        "pro_labore", "Fulano de Tal", "09/2026",
    ) == "pro_labore - Fulano de Tal"


def test_descricao_folha_sem_regra_usa_competencia_em_mm_aaaa():
    # Mesmo com a IA devolvendo a competência invertida.
    assert agente_folha._descricao_tipo_sem_regra(
        "beneficios", None, "2026/09",
    ) == "beneficios - 09/2026"


def test_descricao_folha_sem_regra_cai_para_data_em_dd_mm_aaaa():
    assert agente_folha._descricao_tipo_sem_regra(
        "beneficios", None, None, date(2026, 9, 14),
    ) == "beneficios - 14/09/2026"


# --- regressão: data invertida indo para a API -----------------------------------

def test_converter_data_do_formato_br():
    assert agente_integrado.converter_data("14/09/2026") == "2026-09-14"


def test_converter_data_nao_inverte_resposta_iso_da_ia():
    # Antes: "2026/09/14".split("/") -> dia="2026", ano="14" -> "14-09-2026",
    # silenciosamente, e essa string virava o vencimento do lançamento.
    assert agente_integrado.converter_data("2026/09/14") == "2026-09-14"
    assert agente_integrado.converter_data("2026-09-14") == "2026-09-14"


def test_converter_data_levanta_erro_claro_em_vez_de_lancar_data_errada():
    with pytest.raises(ValueError, match="formato inesperado"):
        agente_integrado.converter_data("a combinar")
