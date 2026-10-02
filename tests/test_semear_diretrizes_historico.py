from src.utils import semear_diretrizes_historico as semear


def _item(descricao, categoria=None, centro_custo=None, fornecedor=None):
    return {
        "descricao": descricao,
        "categorias": [{"nome": categoria}] if categoria else [],
        "centros_de_custo": [{"nome": centro_custo}] if centro_custo else [],
        "fornecedor": {"nome": fornecedor} if fornecedor else {},
    }


def test_extrai_palavras_ignora_curtas_numeros_e_stopwords():
    palavras = semear._extrair_palavras("Hotel GOL NF-e 4521 de Salvador para o Rio, com conforme referente a maio")
    assert "hotel" in palavras
    assert "salvador" in palavras
    assert "4521" not in palavras  # números descartados
    assert "de" not in palavras  # curta demais (< 4 letras)
    assert "conforme" not in palavras  # stopword
    assert "referente" not in palavras  # stopword


def test_minerar_diretrizes_aceita_candidato_forte():
    itens = [
        _item("Reembolso uber corrida aeroporto", "4.15.002. Viagens - A Recuperar", fornecedor="Agencia A"),
        _item("Uber para reuniao com cliente", "4.15.002. Viagens - A Recuperar", fornecedor="Agencia B"),
        _item("Corrida de uber ida volta", "4.15.002. Viagens - A Recuperar", fornecedor="Agencia C"),
    ]
    candidatos = semear.minerar_diretrizes(itens, min_ocorrencias=3, min_fornecedores=2, min_confianca=0.8)
    palavras = {c["palavra"]: c for c in candidatos}
    assert "uber" in palavras
    assert palavras["uber"]["categoria_nome"] == "4.15.002. Viagens - A Recuperar"
    assert palavras["uber"]["ocorrencias"] == 3
    assert palavras["uber"]["fornecedores_distintos"] == 3


def test_minerar_diretrizes_descarta_abaixo_do_minimo_de_ocorrencias():
    itens = [
        _item("Combustivel posto ipiranga", "4.15.002. Viagens - A Recuperar", fornecedor="Posto A"),
        _item("Combustivel abastecimento carro", "4.15.002. Viagens - A Recuperar", fornecedor="Posto B"),
    ]
    candidatos = semear.minerar_diretrizes(itens, min_ocorrencias=3, min_fornecedores=2, min_confianca=0.8)
    assert not any(c["palavra"] == "combustivel" for c in candidatos)


def test_minerar_diretrizes_descarta_fornecedor_unico():
    # Mesma palavra em 3 lançamentos, mas sempre do MESMO fornecedor - deve
    # ser descartada mesmo com ocorrências suficientes (provável artefato
    # do texto de UM fornecedor só, não um padrão generalizável).
    itens = [
        _item("Mensalidade academia fitplace", "4.03.001. Confraternizações e Presentes", fornecedor="Fitplace Ltda")
        for _ in range(3)
    ]
    candidatos = semear.minerar_diretrizes(itens, min_ocorrencias=3, min_fornecedores=2, min_confianca=0.8)
    assert not any(c["palavra"] == "fitplace" for c in candidatos)


def test_minerar_diretrizes_descarta_categoria_ambigua():
    # "despesa" aparece com frequência suficiente e fornecedores distintos,
    # mas espalhada por categorias diferentes sem uma dominante clara.
    itens = [
        _item("Despesa de viagem", "4.15.002. Viagens - A Recuperar", fornecedor="A"),
        _item("Despesa de informatica", "4.06.023. Itens de Informática", fornecedor="B"),
        _item("Despesa de escritorio", "4.06.013. Manutenção do escritório e/ república", fornecedor="C"),
    ]
    candidatos = semear.minerar_diretrizes(itens, min_ocorrencias=3, min_fornecedores=2, min_confianca=0.8)
    assert not any(c["palavra"] == "despesa" for c in candidatos)


def test_minerar_diretrizes_exclui_palavra_que_e_nome_de_fornecedor():
    # "carvalho" aparece em vários lançamentos de "Viagens" processados por
    # agências distintas - passaria nos filtros estatísticos, mas também
    # aparece como nome de fornecedor em outro lançamento do MESMO lote, o
    # que é o sinal de que na verdade é nome de pessoa, não um tipo de
    # despesa generalizável.
    itens = [
        _item("Passagem aerea carvalho", "4.15.002. Viagens - A Recuperar", fornecedor="Agencia X"),
        _item("Hospedagem hotel carvalho", "4.15.002. Viagens - A Recuperar", fornecedor="Agencia Y"),
        _item("Reembolso viagem carvalho", "4.15.002. Viagens - A Recuperar", fornecedor="Agencia Z"),
        _item("Pagamento fornecedor", "4.06.023. Itens de Informática", fornecedor="Carvalho Transportes"),
    ]
    candidatos = semear.minerar_diretrizes(itens, min_ocorrencias=3, min_fornecedores=2, min_confianca=0.8)
    assert not any(c["palavra"] == "carvalho" for c in candidatos)


def test_minerar_diretrizes_usa_centro_custo_quando_unico_e_consistente():
    itens = [
        _item("Aluguel escritorio itagiba", "4.15.001. Moradia - A Recuperar", "3213 - Itagibá - Atlantic Nickel", "Imobiliaria A"),
        _item("Aluguel casa itagiba", "4.15.001. Moradia - A Recuperar", "3213 - Itagibá - Atlantic Nickel", "Imobiliaria B"),
        _item("Aluguel apartamento itagiba", "4.15.001. Moradia - A Recuperar", "3213 - Itagibá - Atlantic Nickel", "Imobiliaria C"),
    ]
    candidatos = semear.minerar_diretrizes(itens, min_ocorrencias=3, min_fornecedores=2, min_confianca=0.8)
    palavras = {c["palavra"]: c for c in candidatos}
    assert palavras["aluguel"]["centro_custo_nome"] == "3213 - Itagibá - Atlantic Nickel"


def test_minerar_diretrizes_ignora_centro_custo_quando_rateio_multiplo():
    item_rateado = _item("Aluguel escritorio", "4.15.001. Moradia - A Recuperar", fornecedor="Imobiliaria A")
    item_rateado["centros_de_custo"] = [{"nome": "Centro A"}, {"nome": "Centro B"}]
    itens = [
        item_rateado,
        _item("Aluguel casa", "4.15.001. Moradia - A Recuperar", "Centro A", "Imobiliaria B"),
        _item("Aluguel apartamento", "4.15.001. Moradia - A Recuperar", "Centro A", "Imobiliaria C"),
    ]
    candidatos = semear.minerar_diretrizes(itens, min_ocorrencias=3, min_fornecedores=2, min_confianca=0.8)
    palavras = {c["palavra"]: c for c in candidatos}
    # só 2 dos 3 lançamentos têm centro de custo único e consistente = 67%,
    # abaixo do min_confianca padrão de 0.8 - não deve sugerir centro nenhum.
    assert palavras["aluguel"]["centro_custo_nome"] is None
