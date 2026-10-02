import threading

from src.utils import fornecedores_registry as fr
from src.utils.fornecedores_registry import normalizar_cnpj


def test_normalizar_cnpj_com_pontuacao():
    assert normalizar_cnpj("12.345.678/0001-90") == "12345678000190"


def test_normalizar_cnpj_sem_pontuacao():
    assert normalizar_cnpj("12345678000190") == "12345678000190"


def test_normalizar_cnpj_com_espacos_e_pontuacao_mista():
    assert normalizar_cnpj(" 12.345.678/0001-90 ") == "12345678000190"


def test_normalizar_cnpj_invalido_poucos_digitos_retorna_none():
    assert normalizar_cnpj("123.456-78") is None


def test_normalizar_cnpj_invalido_digitos_demais_retorna_none():
    assert normalizar_cnpj("123456780001900000") is None


def test_normalizar_cnpj_none_retorna_none():
    assert normalizar_cnpj(None) is None


def test_normalizar_cnpj_vazio_retorna_none():
    assert normalizar_cnpj("") is None


def test_registrar_confirmado_concorrente_nao_perde_atualizacoes(tmp_path, monkeypatch):
    # Regressão (2026-09-03, risco real apontado em auditoria): mesmo
    # cenário de test_historico_centros_funcionarios.py, aplicado ao
    # registro de fornecedores - N threads confirmando o MESMO CNPJ
    # simultaneamente, "vezes_usado" final precisa ser exatamente N, e as
    # threads não podem travar (deadlock) nem lançar exceção.
    monkeypatch.setattr(fr, "CAMINHO_REGISTRO", tmp_path / "fornecedores_registry.json")

    N_THREADS = 25
    erros = []

    def _confirmar(i):
        try:
            fr.registrar_confirmado(
                cnpj="12.345.678/0001-90", nome="Fornecedor Teste",
                contato_id="contato-1", contato_nome="Fornecedor Teste",
                categoria_id="cat-1", categoria_nome="4.01.001. Teste",
            )
        except Exception as e:
            erros.append(e)

    threads = [threading.Thread(target=_confirmar, args=(i,)) for i in range(N_THREADS)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert not any(t.is_alive() for t in threads), "uma ou mais threads ainda vivas - indício de deadlock no lock"
    assert not erros, f"threads lançaram exceção inesperada: {erros}"
    assert fr.buscar_por_cnpj("12345678000190")["vezes_usado"] == N_THREADS


def test_registrar_banco_pagamento_sem_boleto_grava_no_mesmo_registro_do_cnpj(tmp_path, monkeypatch):
    monkeypatch.setattr(fr, "CAMINHO_REGISTRO", tmp_path / "fornecedores_registry.json")

    fr.registrar_confirmado(
        cnpj="12.345.678/0001-90", nome="Fornecedor Teste",
        contato_id="contato-1", contato_nome="Fornecedor Teste",
        categoria_id="cat-1", categoria_nome="4.01.001. Teste",
    )
    fr.registrar_banco_pagamento_sem_boleto("12.345.678/0001-90", "beta", origem="aprovado_humano")

    registro = fr.buscar_por_cnpj("12345678000190")
    assert registro["banco_pagamento_sem_boleto"] == "beta"
    assert registro["origem_banco"] == "aprovado_humano"
    # Não apagou o que já existia (contato/categoria) - só acrescentou.
    assert registro["contato_id"] == "contato-1"
    assert registro["categoria_id"] == "cat-1"


def test_registrar_confirmado_nao_apaga_banco_ja_aprendido(tmp_path, monkeypatch):
    monkeypatch.setattr(fr, "CAMINHO_REGISTRO", tmp_path / "fornecedores_registry.json")

    fr.registrar_banco_pagamento_sem_boleto("12.345.678/0001-90", "alfa", origem="inferido_do_erp")
    fr.registrar_confirmado(
        cnpj="12.345.678/0001-90", nome="Fornecedor Teste",
        contato_id="contato-1", contato_nome="Fornecedor Teste",
        categoria_id="cat-1", categoria_nome="4.01.001. Teste",
    )

    registro = fr.buscar_por_cnpj("12345678000190")
    assert registro["banco_pagamento_sem_boleto"] == "alfa"
    assert registro["origem_banco"] == "inferido_do_erp"


def test_registrar_banco_pagamento_invalido_nao_grava(tmp_path, monkeypatch):
    monkeypatch.setattr(fr, "CAMINHO_REGISTRO", tmp_path / "fornecedores_registry.json")

    fr.registrar_banco_pagamento_sem_boleto("12.345.678/0001-90", "bradesco")

    assert fr.buscar_por_cnpj("12345678000190") is None
