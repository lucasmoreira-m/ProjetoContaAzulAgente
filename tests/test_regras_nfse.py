from datetime import date

from src.utils import regras_nfse


def test_calcular_vencimento_pula_fim_de_semana():
    # date.fromisocalendar(ano, semana, 1) é SEMPRE segunda-feira, por
    # definição do calendário ISO - não depende de saber de cor o
    # calendário real de nenhum ano específico.
    segunda = date.fromisocalendar(2026, 10, 1)

    vencimento = regras_nfse.calcular_vencimento_dia_util(segunda, n=5)

    # Ter, Qua, Qui, Sex = 4 dias úteis; Sáb/Dom pulados; Seg seguinte = 5º.
    assert vencimento == date.fromordinal(segunda.toordinal() + 7)
    assert vencimento.weekday() == 0  # segunda-feira


def test_calcular_vencimento_pula_feriado_fixo(monkeypatch):
    segunda = date.fromisocalendar(2026, 20, 1)
    terca = date.fromordinal(segunda.toordinal() + 1)
    monkeypatch.setattr(regras_nfse, "_FERIADOS_NACIONAIS_FIXOS", {(terca.month, terca.day)})

    vencimento = regras_nfse.calcular_vencimento_dia_util(segunda, n=1)

    # Terça é feriado (monkeypatchado) - o 1º dia útil real é quarta.
    assert vencimento == date.fromordinal(terca.toordinal() + 1)


def test_montar_descricao_nfse_com_tipo_servico():
    assert regras_nfse.montar_descricao_nfse("123", "Consultoria") == "NFS-e 123 - Consultoria"


def test_montar_descricao_nfse_sem_tipo_servico():
    assert regras_nfse.montar_descricao_nfse("123", None) == "NFS-e 123"


def test_montar_observacao_nfse_inclui_discriminacao_e_codigo():
    observacao = regras_nfse.montar_observacao_nfse(
        "Serviços de consultoria em engenharia", "07.05", "Engenharia, agronomia, arquitetura",
    )
    assert "Serviços de consultoria em engenharia" in observacao
    assert "07.05" in observacao
    assert "Engenharia, agronomia, arquitetura" in observacao
