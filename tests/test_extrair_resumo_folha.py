from src.agentes.extrair_resumo_folha import remover_linhas_totais


def test_mantem_linha_com_placeholder_a_definir():
    rateio = [{"centro_custo": "A_DEFINIR", "valor": 1993.0}]
    assert remover_linhas_totais(rateio) == rateio


def test_remove_linha_de_total():
    rateio = [
        {"centro_custo": "3233 - Cedro - Hospitais", "valor": 100.0},
        {"centro_custo": "Empresa Total", "valor": 100.0},
    ]
    filtrado = remover_linhas_totais(rateio)
    assert filtrado == [{"centro_custo": "3233 - Cedro - Hospitais", "valor": 100.0}]


def test_mantem_linha_com_codigo_numerico_valido():
    rateio = [{"centro_custo": "1122 - Administrativo", "valor": 50.0}]
    assert remover_linhas_totais(rateio) == rateio


def test_mantem_linha_com_codigo_numerico_sem_hifen_nem_nome():
    # Caso real observado em produção: anotação visual traz só o código
    # (ex: "C. RESP: 1122"), sem "- Administrativo" atrás.
    rateio = [{"centro_custo": "1122", "valor": 1993.0}]
    assert remover_linhas_totais(rateio) == rateio


def test_remove_linha_sem_codigo_e_sem_ser_placeholder():
    rateio = [{"centro_custo": "Observação qualquer", "valor": 10.0}]
    assert remover_linhas_totais(rateio) == []
