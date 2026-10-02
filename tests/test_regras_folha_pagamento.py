from datetime import date

from src.utils.regras_folha_pagamento import (
    RATEIO_PADRAO_FOLHA,
    ajustar_para_dia_util_anterior,
    dia_15_do_mes,
    dia_20_mes_seguinte,
    ultimo_dia_util_do_mes,
)


def test_ajustar_para_dia_util_anterior_sabado_vai_para_sexta():
    assert ajustar_para_dia_util_anterior(date(2026, 1, 31)) == date(2026, 1, 30)


def test_ajustar_para_dia_util_anterior_domingo_vai_para_sexta():
    assert ajustar_para_dia_util_anterior(date(2026, 5, 31)) == date(2026, 5, 29)


def test_ajustar_para_dia_util_anterior_dia_util_nao_muda():
    assert ajustar_para_dia_util_anterior(date(2026, 3, 31)) == date(2026, 3, 31)


def test_ultimo_dia_util_do_mes_ajusta_quando_cai_no_sabado():
    assert ultimo_dia_util_do_mes(2026, 1) == date(2026, 1, 30)


def test_ultimo_dia_util_do_mes_ajusta_quando_cai_no_domingo():
    assert ultimo_dia_util_do_mes(2026, 5) == date(2026, 5, 29)


def test_ultimo_dia_util_do_mes_mantem_quando_ja_e_dia_util():
    assert ultimo_dia_util_do_mes(2026, 8) == date(2026, 8, 31)


def test_dia_20_mes_seguinte_ajusta_fim_de_semana():
    # dezembro/2026 é o mês seguinte a novembro/2026; dia 20/12/2026 cai num domingo.
    assert dia_20_mes_seguinte(2026, 11) == date(2026, 12, 18)


def test_dia_20_mes_seguinte_virada_de_ano_em_dezembro():
    # competência dezembro/2026 -> mês seguinte é janeiro/2027 (ano vira).
    assert dia_20_mes_seguinte(2026, 12) == date(2027, 1, 20)


def test_dia_15_do_mes_ajusta_fim_de_semana():
    assert dia_15_do_mes(2026, 2) == date(2026, 2, 13)


def test_dia_15_do_mes_mantem_quando_ja_e_dia_util():
    assert dia_15_do_mes(2026, 1) == date(2026, 1, 15)


def test_rateio_padrao_folha_soma_100_por_cento():
    soma = sum(item["percentual"] for item in RATEIO_PADRAO_FOLHA)
    assert abs(soma - 100) <= 0.05
