"""Teto de valor para lançamento automático (2026-09-25) - ver src/utils/teto_autonomia.py."""

from src.core import config
from src.utils import teto_autonomia as ta


def test_tetos_definidos_pelo_usuario():
    assert config.TETO_AUTONOMIA_NFSE == 45000
    assert config.TETO_AUTONOMIA_DOCUMENTO_COMUM == 16000


def test_dentro_do_teto_ou_exatamente_no_limite_nao_bloqueia():
    assert ta.motivo_acima_do_teto(41173.80, ta.TIPO_NFSE) is None  # maior NFS-e recorrente (M2)
    assert ta.motivo_acima_do_teto(45000.00, ta.TIPO_NFSE) is None
    assert ta.motivo_acima_do_teto(15148.16, ta.TIPO_DOCUMENTO_COMUM) is None  # condomínio mensal
    assert ta.motivo_acima_do_teto(16000.00, ta.TIPO_DOCUMENTO_COMUM) is None
    assert ta.motivo_acima_do_teto(None, ta.TIPO_NFSE) is None


def test_acima_do_teto_explica_que_e_medida_de_seguranca_por_valor_nao_recorrente():
    motivo = ta.motivo_acima_do_teto(98765.00, ta.TIPO_NFSE)
    assert motivo.startswith("MEDIDA DE SEGURANÇA - VALOR FORA DO PADRÃO RECORRENTE")
    assert "R$ 98.765,00" in motivo and "R$ 45.000,00" in motivo
    assert "não é recorrente" in motivo and "NFS-e" in motivo
    motivo_comum = ta.motivo_acima_do_teto(30087.95, ta.TIPO_DOCUMENTO_COMUM)
    assert "R$ 30.087,95" in motivo_comum and "R$ 16.000,00" in motivo_comum


def test_formatar_reais():
    assert ta.formatar_reais(1234567.8) == "R$ 1.234.567,80"
    assert ta.formatar_reais(0.5) == "R$ 0,50"


def test_saude_isenta_do_teto_pelo_cnpj_com_ou_sem_pontuacao():
    # Decisão do usuário (2026-09-25): plano Saúde Exemplo passa de R$ 150 mil/mês e lança normalmente.
    assert ta.motivo_acima_do_teto(162992.24, ta.TIPO_NFSE, "44.555.666/0001-77") is None
    assert ta.motivo_acima_do_teto(162992.24, ta.TIPO_NFSE, "44555666000177") is None
    assert ta.motivo_acima_do_teto(162992.24, ta.TIPO_DOCUMENTO_COMUM, "44.555.666/0001-77") is None


def test_isencao_nao_vale_para_outro_cnpj_nem_sem_cnpj():
    assert ta.motivo_acima_do_teto(162992.24, ta.TIPO_NFSE, "12.345.678/0001-90")
    assert ta.motivo_acima_do_teto(162992.24, ta.TIPO_NFSE, None)
    assert ta.motivo_acima_do_teto(162992.24, ta.TIPO_NFSE, "")
