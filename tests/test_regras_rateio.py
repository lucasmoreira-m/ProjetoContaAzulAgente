from src.utils import regras_rateio
from src.utils.regras_rateio import buscar_regra_rateio, calcular_rateio


def test_calcular_rateio_soma_bate_com_total_valor_redondo():
    regra = [
        {"centro_custo_id": "a", "centro_custo_nome": "Comercial", "percentual": 40},
        {"centro_custo_id": "b", "centro_custo_nome": "Administrativo", "percentual": 35},
        {"centro_custo_id": "c", "centro_custo_nome": "Financeiro", "percentual": 25},
    ]
    splits = calcular_rateio(1000.00, regra)

    assert [item["valor"] for item in splits] == [400.00, 350.00, 250.00]
    assert round(sum(item["valor"] for item in splits), 2) == 1000.00


def test_calcular_rateio_ultimo_item_absorve_centavos_de_arredondamento():
    regra = [
        {"centro_custo_id": "a", "centro_custo_nome": "Comercial", "percentual": 40},
        {"centro_custo_id": "b", "centro_custo_nome": "Administrativo", "percentual": 35},
        {"centro_custo_id": "c", "centro_custo_nome": "Financeiro", "percentual": 25},
    ]
    splits = calcular_rateio(100.01, regra)

    # 40% e 35% de 100.01 arredondam para 40.00 e 35.00; o resto (25.01, não
    # 25.00) precisa ficar todo no último item para a soma bater exatamente.
    assert splits[-1]["valor"] == 25.01
    assert round(sum(item["valor"] for item in splits), 2) == 100.01


def test_calcular_rateio_preserva_metadados_do_centro_de_custo():
    regra = [{"centro_custo_id": "x1", "centro_custo_nome": "Financeiro", "percentual": 100}]
    splits = calcular_rateio(500.0, regra)

    assert splits == [{
        "centro_custo_id": "x1",
        "centro_custo_nome": "Financeiro",
        "percentual": 100,
        "valor": 500.0,
    }]


def test_buscar_regra_rateio_match_exato_ignora_caixa_e_acento(monkeypatch):
    # REGRAS_RATEIO é dado de negócio real (hoje vazio, esperando as regras
    # da empresa) - não testamos contra ele diretamente, injetamos uma regra só
    # para este teste, para não depender de dado que muda com o tempo.
    regra_teste = [{"centro_custo_id": "x", "centro_custo_nome": "Comercial", "percentual": 100}]
    monkeypatch.setitem(regras_rateio.REGRAS_RATEIO, "fornecedor de teste ltda", regra_teste)

    regra = buscar_regra_rateio("FORNECEDOR DE TESTE LTDA")
    assert regra is not None
    assert regra[0]["centro_custo_nome"] == "Comercial"


def test_buscar_regra_rateio_match_aproximado_por_substring(monkeypatch):
    regra_teste = [{"centro_custo_id": "x", "centro_custo_nome": "Comercial", "percentual": 100}]
    monkeypatch.setitem(regras_rateio.REGRAS_RATEIO, "fornecedor de teste ltda", regra_teste)

    regra = buscar_regra_rateio("Loja Fornecedor de Teste Ltda - Filial Centro")
    assert regra is not None


def test_buscar_regra_rateio_nao_encontrado_retorna_none():
    assert buscar_regra_rateio("Fornecedor Qualquer Sem Regra Cadastrada") is None


def test_buscar_regra_rateio_nao_faz_match_parcial_com_chave_curta(monkeypatch):
    # "sp" é literalmente uma substring de "espaco..." - sem o mínimo de 5
    # caracteres, isso bateria por engano com um fornecedor sem relação
    # nenhuma com a regra cadastrada para "sp".
    regra_teste = [{"centro_custo_id": "x", "centro_custo_nome": "Comercial", "percentual": 100}]
    monkeypatch.setitem(regras_rateio.REGRAS_RATEIO, "sp", regra_teste)

    assert buscar_regra_rateio("Espaço Fornecedor Ltda") is None


def test_buscar_regra_rateio_nao_faz_match_parcial_com_nome_buscado_curto(monkeypatch):
    # "cab" é literalmente uma substring de "cabral engenharia ltda" - sem o
    # mínimo de 5 caracteres do lado buscado, uma busca truncada/errada
    # bateria por engano com essa regra.
    regra_teste = [{"centro_custo_id": "x", "centro_custo_nome": "Comercial", "percentual": 100}]
    monkeypatch.setitem(regras_rateio.REGRAS_RATEIO, "cabral engenharia ltda", regra_teste)

    assert buscar_regra_rateio("cab") is None
