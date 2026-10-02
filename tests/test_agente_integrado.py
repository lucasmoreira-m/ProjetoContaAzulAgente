from src.agentes import agente_integrado as ai


def _dados_base(**overrides) -> dict:
    base = {
        "tipo_documento": "conta_fixa",
        "fornecedor": "Fulano de Tal",
        "cnpj": None,
        "valor_total": 500.0,
        "data_vencimento": "10/09/2026",
        "numero_documento": None,
        "categoria_sugerida": "Viagens",
        "conta_financeira_sugerida": None,
        "centro_custo_sugerido": None,
        "palavras_chave_identificadas": [],
        "rateio_centro_custo_documento": [],
        "observacoes": None,
    }
    base.update(overrides)
    return base


def _mocks_lancamento_generico_sempre_resolve(monkeypatch, dados_extraidos):
    """Mocka toda a cadeia (extração, categoria, contato, duplicidade,
    token) para sempre resolver com sucesso - isola só o efeito das regras
    de autonomia/diretrizes sob teste.

    Por padrão simula um fornecedor Camada 1 (registro local) JÁ MADURO -
    isola o efeito da funcionalidade sob teste (diretrizes globais,
    autonomia zero, etc.) do gate de maturação do Funil de Decisão
    Contábil (ver testes dedicados a Camada 1/2/3 mais abaixo)."""
    monkeypatch.setattr(ai, "extrair_dados_documento", lambda caminho, max_tentativas=5: dados_extraidos)
    monkeypatch.setattr(ai, "buscar_categoria", lambda access_token, nome: ({"id": "cat1", "nome": nome}, "exato"))
    monkeypatch.setattr(
        ai, "buscar_contato_com_aprendizado",
        lambda access_token, nome, cnpj: ({"id": "c1", "nome": nome}, "exato"),
    )
    monkeypatch.setattr(ai, "obter_access_token", lambda: "token-fake")
    monkeypatch.setattr(
        ai.verificacao_duplicidade, "verificar_lancamento_similar_existe",
        lambda *a, **k: None,
    )
    monkeypatch.setattr(ai.fornecedores_registry, "registrar_confirmado", lambda **k: None)
    monkeypatch.setattr(
        ai.fornecedores_registry, "buscar_por_cnpj",
        lambda cnpj: {"categoria_id": "cat1", "categoria_nome": dados_extraidos["categoria_sugerida"]},
    )
    monkeypatch.setattr(ai.historico_centros_funcionarios, "padrao_esta_maduro", lambda padrao: True)


def test_prestacao_contas_nunca_lanca_automatico_mesmo_com_tudo_resolvendo(monkeypatch):
    # Regressão (2026-09-03, "autonomia zero" pedida explicitamente pelo
    # usuário): reembolso/prestação de contas NUNCA pode lançar automático,
    # mesmo com categoria/contato/rateio todos resolvendo com match exato.
    dados = _dados_base(tipo_documento="prestacao_contas")
    _mocks_lancamento_generico_sempre_resolve(monkeypatch, dados)

    chamou_criar_lancamento = []
    monkeypatch.setattr(
        ai, "criar_lancamento",
        lambda *a, **k: (chamou_criar_lancamento.append(1) or (200, {"protocolo": "nao-deveria-lancar"})),
    )

    resultado = ai.processar_documento("qualquer.pdf")

    assert resultado["sucesso"] is False
    assert "autonomia zero" in resultado["motivo"].lower()
    assert not chamou_criar_lancamento


def test_conta_fixa_continua_lancando_automatico_quando_tudo_resolve(monkeypatch):
    # Regressão: a nova trava de "autonomia zero" é EXCLUSIVA de
    # prestacao_contas - conta_fixa continua se comportando exatamente
    # como antes (lança automático quando tudo resolve).
    dados = _dados_base(tipo_documento="conta_fixa")
    _mocks_lancamento_generico_sempre_resolve(monkeypatch, dados)
    monkeypatch.setattr(ai, "criar_lancamento", lambda *a, **k: (200, {"protocolo": "proto-conta-fixa"}))

    resultado = ai.processar_documento("qualquer.pdf")

    assert resultado["sucesso"] is True
    assert resultado["detalhes"]["protocolo"] == "proto-conta-fixa"


def test_centro_custo_sugerido_por_diretriz_global_usado_quando_sem_rateio_e_match_exato(monkeypatch):
    # Diretrizes Globais de Texto: sem rateio explícito no documento e sem
    # regra de negócio cadastrada, mas com um centro_custo_sugerido (vindo
    # do cruzamento com diretrizes globais por palavra-chave) que casa
    # exatamente na Conta Azul - usa esse centro para o valor inteiro.
    dados = _dados_base(tipo_documento="conta_fixa", centro_custo_sugerido="1122 - Administrativo")
    _mocks_lancamento_generico_sempre_resolve(monkeypatch, dados)
    monkeypatch.setattr(ai, "buscar_regra_rateio", lambda nome: None)
    monkeypatch.setattr(ai, "buscar_centro_custo", lambda access_token, nome: ({"id": "centro1", "nome": nome}, "exato"))

    capturado = {}

    def fake_criar_lancamento(access_token, dados_lanc, categoria_id, contato_id, data_venc, **kwargs):
        capturado.update(kwargs)
        return 200, {"protocolo": "proto-centro"}

    monkeypatch.setattr(ai, "criar_lancamento", fake_criar_lancamento)

    resultado = ai.processar_documento("qualquer.pdf")

    assert resultado["sucesso"] is True
    assert capturado["rateio_centro_custo"] == [{
        "centro_custo_id": "centro1", "centro_custo_nome": "1122 - Administrativo",
        "percentual": None, "valor": 500.0,
    }]


def test_conta_financeira_sugerida_por_diretriz_global_usada_quando_match_exato(monkeypatch):
    dados = _dados_base(tipo_documento="conta_fixa", conta_financeira_sugerida="Banco Alfa - Matriz")
    _mocks_lancamento_generico_sempre_resolve(monkeypatch, dados)
    monkeypatch.setattr(ai, "buscar_regra_rateio", lambda nome: None)
    monkeypatch.setattr(ai, "buscar_conta_financeira", lambda access_token, nome: ({"id": "conta-alfa", "nome": nome}, "exato"))

    capturado = {}

    def fake_criar_lancamento(access_token, dados_lanc, categoria_id, contato_id, data_venc, **kwargs):
        capturado.update(kwargs)
        return 200, {"protocolo": "proto-conta"}

    monkeypatch.setattr(ai, "criar_lancamento", fake_criar_lancamento)

    resultado = ai.processar_documento("qualquer.pdf")

    assert resultado["sucesso"] is True
    assert capturado["conta_financeira_id"] == "conta-alfa"


def test_categoria_aprendida_por_cnpj_usada_quando_ia_concorda(monkeypatch):
    # Regressão (2026-09-04, pedido explícito do usuário): CNPJ já
    # confirmado antes com uma categoria - a IA sugere a MESMA categoria de
    # novo (caso comum) - usa direto, marca origem "aprendida".
    dados = _dados_base(cnpj="12345678000190", categoria_sugerida="4.01.002. Aluguel")
    _mocks_lancamento_generico_sempre_resolve(monkeypatch, dados)
    monkeypatch.setattr(
        ai.fornecedores_registry, "buscar_por_cnpj",
        lambda cnpj: {"categoria_id": "cat1", "categoria_nome": "4.01.002. Aluguel"},
    )
    monkeypatch.setattr(ai, "criar_lancamento", lambda *a, **k: (200, {"protocolo": "proto-aprendida"}))

    resultado = ai.processar_documento("qualquer.pdf")

    assert resultado["sucesso"] is True
    assert resultado["detalhes"]["categoria"]["id"] == "cat1"
    assert resultado["detalhes"]["origem_categoria"] == "aprendida"


def test_categoria_aprendida_por_cnpj_prevalece_quando_ia_discorda(monkeypatch):
    # A IA sugere uma categoria DIFERENTE da que já tínhamos confirmado
    # para este CNPJ - a categoria aprendida (confirmada por lançamento
    # anterior) prevalece sobre o palpite novo, sem revisão humana.
    dados = _dados_base(cnpj="12345678000190", categoria_sugerida="Categoria Errada Qualquer")
    _mocks_lancamento_generico_sempre_resolve(monkeypatch, dados)
    # buscar_categoria (mock padrão) resolveria "Categoria Errada Qualquer"
    # com id "cat1" por match exato - mas a categoria aprendida tem OUTRO id.
    monkeypatch.setattr(
        ai.fornecedores_registry, "buscar_por_cnpj",
        lambda cnpj: {"categoria_id": "cat-aprendida-id", "categoria_nome": "4.01.002. Aluguel"},
    )
    monkeypatch.setattr(ai, "criar_lancamento", lambda *a, **k: (200, {"protocolo": "proto-prevalece"}))

    resultado = ai.processar_documento("qualquer.pdf")

    assert resultado["sucesso"] is True
    assert resultado["detalhes"]["categoria"]["id"] == "cat-aprendida-id"
    assert resultado["detalhes"]["categoria"]["nome"] == "4.01.002. Aluguel"
    assert resultado["detalhes"]["origem_categoria"] == "aprendida"


def test_categoria_extraida_novo_quando_cnpj_desconhecido_e_sem_historico_erp_vai_para_revisao(monkeypatch):
    # CAMADA 3 (2026-09-04, pedido explícito do usuário): sem registro
    # local (Camada 1) E sem padrão de repetição no histórico ao vivo da
    # Conta Azul (Camada 2) - usa o palpite da IA, mas NUNCA lança
    # automático, sempre para revisão humana (tarja amarela no painel).
    dados = _dados_base(cnpj="99999999000199", categoria_sugerida="4.02.001. Qualquer")
    _mocks_lancamento_generico_sempre_resolve(monkeypatch, dados)
    monkeypatch.setattr(ai.fornecedores_registry, "buscar_por_cnpj", lambda cnpj: None)
    # Camada 2 não encontrou nenhum padrão de repetição no ERP.
    monkeypatch.setattr(ai, "_tentar_inferir_padrao_do_erp", lambda *a, **k: None)
    chamou_criar_lancamento = []
    monkeypatch.setattr(
        ai, "criar_lancamento",
        lambda *a, **k: (chamou_criar_lancamento.append(1) or (200, {"protocolo": "nao-deveria-lancar"})),
    )

    resultado = ai.processar_documento("qualquer.pdf")

    assert resultado["sucesso"] is False
    assert not chamou_criar_lancamento
    assert resultado["detalhes"]["origem_categoria"] == "extraida_novo"
    assert resultado["detalhes"]["camada_decisao"] == "camada3_fallback_ia"
    assert "Camada 3" in resultado["motivo"] or "inédito" in resultado["motivo"].lower()


def test_camada1_registro_local_nao_maduro_vai_para_revisao_mesmo_com_tudo_resolvendo(monkeypatch):
    # CAMADA 1 (2026-09-04, pedido explícito do usuário): CNPJ já
    # confirmado localmente, mas o padrão AINDA NÃO maturou (menos de 3
    # aprovações manuais no painel para este fornecedor) - categoria e
    # contato resolvem certinho, mas não lança automático; fica em
    # revisão até acumular aprovações suficientes.
    dados = _dados_base(cnpj="12345678000190", categoria_sugerida="4.01.002. Aluguel")
    _mocks_lancamento_generico_sempre_resolve(monkeypatch, dados)
    monkeypatch.setattr(
        ai.fornecedores_registry, "buscar_por_cnpj",
        lambda cnpj: {"categoria_id": "cat1", "categoria_nome": "4.01.002. Aluguel"},
    )
    monkeypatch.setattr(ai.historico_centros_funcionarios, "padrao_esta_maduro", lambda padrao: False)
    monkeypatch.setattr(ai.historico_centros_funcionarios, "contagem_aprovacoes_padrao", lambda padrao: 1)
    chamou_criar_lancamento = []
    monkeypatch.setattr(
        ai, "criar_lancamento",
        lambda *a, **k: (chamou_criar_lancamento.append(1) or (200, {"protocolo": "nao-deveria-lancar"})),
    )

    resultado = ai.processar_documento("qualquer.pdf")

    assert resultado["sucesso"] is False
    assert not chamou_criar_lancamento
    assert resultado["detalhes"]["origem_categoria"] == "aprendida"
    assert "1/3" in resultado["motivo"]


def test_camada2_padrao_inferido_do_erp_lanca_automatico_e_pre_matura_o_padrao(monkeypatch):
    # CAMADA 2 (2026-09-04, pedido explícito do usuário): CNPJ inédito
    # localmente, mas o histórico ao vivo da Conta Azul mostra um padrão
    # de repetição claro - lança automático JÁ NA PRIMEIRA VEZ (evidência
    # de 3+ lançamentos RECEBIDOS reais é mais forte que aprovação manual
    # no nosso painel) e salva a regra aprendida + pré-matura o padrão.
    dados = _dados_base(cnpj="55566677000188", categoria_sugerida="Qualquer Coisa")
    _mocks_lancamento_generico_sempre_resolve(monkeypatch, dados)
    monkeypatch.setattr(ai.fornecedores_registry, "buscar_por_cnpj", lambda cnpj: None)
    monkeypatch.setattr(
        ai, "_tentar_inferir_padrao_do_erp",
        lambda access_token, contato_id, nome_fornecedor: {
            "categoria": {"id": "cat-erp", "nome": "4.01.005. Manutenção"},
            "rateio": None,
            "evidencia_qtd": 5,
        },
    )
    chamadas_registrar_confirmado = []
    monkeypatch.setattr(
        ai.fornecedores_registry, "registrar_confirmado",
        lambda **kwargs: chamadas_registrar_confirmado.append(kwargs),
    )
    chamadas_aprovacao = []
    monkeypatch.setattr(
        ai.historico_centros_funcionarios, "registrar_aprovacao_padrao_documento",
        lambda padrao, nota=None: chamadas_aprovacao.append(padrao),
    )
    monkeypatch.setattr(ai, "criar_lancamento", lambda *a, **k: (200, {"protocolo": "proto-erp"}))

    resultado = ai.processar_documento("qualquer.pdf")

    assert resultado["sucesso"] is True
    assert resultado["detalhes"]["categoria"]["id"] == "cat-erp"
    assert resultado["detalhes"]["origem_categoria"] == "inferido_do_erp"
    assert resultado["detalhes"]["camada_decisao"] == "camada2_erp_inferido"
    assert resultado["detalhes"]["camada2_evidencia_qtd"] == 5
    assert chamadas_registrar_confirmado[0]["origem"] == "inferido_do_erp"
    assert len(chamadas_aprovacao) == ai.historico_centros_funcionarios.MATURACAO_MINIMA_PARA_AUTONOMIA
    assert all(p == "cnpj:55566677000188" for p in chamadas_aprovacao)


def test_camada2_nao_dispara_com_contato_apenas_aproximado(monkeypatch):
    # CORREÇÃO (2026-09-04, achado durante revisão pós-implementação): com
    # match aproximado (fuzzy por nome) o contato_id pode ser de um
    # cadastro parecido, mas ERRADO - filtrar o histórico do ERP por esse
    # id inferiria (e pré-maturaria!) uma regra para o fornecedor incerto.
    # Camada 2 não deve nem tentar rodar sem match exato de contato.
    dados = _dados_base(cnpj="55566677000188", categoria_sugerida="Qualquer Coisa")
    _mocks_lancamento_generico_sempre_resolve(monkeypatch, dados)
    monkeypatch.setattr(ai.fornecedores_registry, "buscar_por_cnpj", lambda cnpj: None)
    monkeypatch.setattr(
        ai, "buscar_contato_com_aprendizado",
        lambda access_token, nome, cnpj: ({"id": "c1", "nome": nome}, "aproximado"),
    )
    chamou_camada2 = []
    monkeypatch.setattr(
        ai, "_tentar_inferir_padrao_do_erp",
        lambda *a, **k: (chamou_camada2.append(1) or {"categoria": {"id": "x", "nome": "y"}, "rateio": None}),
    )
    chamadas_registrar_confirmado = []
    monkeypatch.setattr(
        ai.fornecedores_registry, "registrar_confirmado",
        lambda **kwargs: chamadas_registrar_confirmado.append(kwargs),
    )

    resultado = ai.processar_documento("qualquer.pdf")

    assert not chamou_camada2, "Camada 2 não deveria nem tentar rodar sem match exato de contato"
    assert not chamadas_registrar_confirmado
    assert resultado["sucesso"] is False
    assert resultado["detalhes"]["camada_decisao"] == "camada3_fallback_ia"


def test_categoria_aprendida_nao_dispara_segunda_chamada_ao_gemini(monkeypatch):
    # Teste explicitamente pedido: fornecedor já visto (CNPJ conhecido) não
    # aciona nenhuma chamada extra a extrair_dados_documento (Gemini) só
    # para resolver/validar a categoria - a extração continua acontecendo
    # exatamente UMA vez, e a reutilização é feita localmente depois.
    chamadas_gemini = []

    def fake_extrair(caminho, max_tentativas=5):
        chamadas_gemini.append(caminho)
        return _dados_base(cnpj="12345678000190", categoria_sugerida="Qualquer Coisa")

    monkeypatch.setattr(ai, "extrair_dados_documento", fake_extrair)
    monkeypatch.setattr(ai, "buscar_categoria", lambda access_token, nome: ({"id": "cat1", "nome": nome}, "exato"))
    monkeypatch.setattr(
        ai, "buscar_contato_com_aprendizado",
        lambda access_token, nome, cnpj: ({"id": "c1", "nome": nome}, "exato"),
    )
    monkeypatch.setattr(ai, "obter_access_token", lambda: "token-fake")
    monkeypatch.setattr(ai.verificacao_duplicidade, "verificar_lancamento_similar_existe", lambda *a, **k: None)
    monkeypatch.setattr(ai.fornecedores_registry, "registrar_confirmado", lambda **k: None)
    monkeypatch.setattr(
        ai.fornecedores_registry, "buscar_por_cnpj",
        lambda cnpj: {"categoria_id": "cat-aprendida-id", "categoria_nome": "4.01.002. Aluguel"},
    )
    monkeypatch.setattr(ai.historico_centros_funcionarios, "padrao_esta_maduro", lambda padrao: True)
    monkeypatch.setattr(ai, "criar_lancamento", lambda *a, **k: (200, {"protocolo": "proto"}))

    resultado = ai.processar_documento("qualquer.pdf")

    assert resultado["sucesso"] is True
    assert resultado["detalhes"]["origem_categoria"] == "aprendida"
    assert len(chamadas_gemini) == 1, "extrair_dados_documento (Gemini) foi chamado mais de uma vez"


def _item_contas_a_pagar(fornecedor_id, status_traduzido="RECEBIDO", categoria_id="catX", categoria_nome="Cat X", centros=None):
    return {
        "fornecedor": {"id": fornecedor_id, "nome": "Fornecedor X"},
        "status_traduzido": status_traduzido,
        "categorias": [{"id": categoria_id, "nome": categoria_nome}],
        "centros_de_custo": centros or [],
    }


def test_inferir_padrao_erp_encontra_padrao_com_3_recebidos_mesma_categoria(monkeypatch):
    itens = [
        _item_contas_a_pagar("c1"), _item_contas_a_pagar("c1"), _item_contas_a_pagar("c1"),
        _item_contas_a_pagar("outro-fornecedor"),  # ruído: outro fornecedor (filtrado por contato_id)
        _item_contas_a_pagar("c1", status_traduzido="PENDENTE"),  # ruído: ainda não RECEBIDO
    ]
    monkeypatch.setattr(ai.ca, "get", lambda *a, **k: {"itens": itens})

    resultado = ai._tentar_inferir_padrao_do_erp("token-fake", "c1", "Fornecedor X")

    assert resultado == {"categoria": {"id": "catX", "nome": "Cat X"}, "rateio": None, "evidencia_qtd": 3}


def test_inferir_padrao_erp_menos_de_3_ocorrencias_retorna_none(monkeypatch):
    itens = [_item_contas_a_pagar("c1"), _item_contas_a_pagar("c1")]
    monkeypatch.setattr(ai.ca, "get", lambda *a, **k: {"itens": itens})

    assert ai._tentar_inferir_padrao_do_erp("token-fake", "c1", "Fornecedor X") is None


def test_inferir_padrao_erp_infere_rateio_com_centro_unico_consistente(monkeypatch):
    centro = [{"id": "centro1", "nome": "Administrativo"}]
    itens = [_item_contas_a_pagar("c1", centros=centro) for _ in range(3)]
    monkeypatch.setattr(ai.ca, "get", lambda *a, **k: {"itens": itens})

    resultado = ai._tentar_inferir_padrao_do_erp("token-fake", "c1", "Fornecedor X")

    assert resultado["rateio"] == [{
        "centro_custo_id": "centro1", "centro_custo_nome": "Administrativo", "percentual": 100.0,
    }]


def test_inferir_padrao_erp_nao_infere_rateio_quando_centros_variam(monkeypatch):
    itens = [
        _item_contas_a_pagar("c1", centros=[{"id": "centro1", "nome": "Adm"}]),
        _item_contas_a_pagar("c1", centros=[{"id": "centro2", "nome": "Comercial"}]),
        _item_contas_a_pagar("c1", centros=[{"id": "centro1", "nome": "Adm"}]),
    ]
    monkeypatch.setattr(ai.ca, "get", lambda *a, **k: {"itens": itens})

    resultado = ai._tentar_inferir_padrao_do_erp("token-fake", "c1", "Fornecedor X")

    assert resultado["rateio"] is None


def test_inferir_padrao_erp_pagina_ate_pagina_incompleta(monkeypatch):
    # A API não filtra por fornecedor (confirmado ao vivo, 2026-09-04) -
    # a função precisa paginar TODO o intervalo de data e filtrar no
    # cliente; a paginação para quando uma página vem com menos itens que
    # tamanho_pagina (200), sinal de fim dos dados.
    pagina1 = [_item_contas_a_pagar("c1") for _ in range(200)]
    pagina2 = [_item_contas_a_pagar("c1"), _item_contas_a_pagar("c1"), _item_contas_a_pagar("c1")]
    chamadas_pagina = []

    def fake_get(access_token, caminho, params=None):
        chamadas_pagina.append(params["pagina"])
        return {"itens": pagina1 if params["pagina"] == 1 else pagina2}

    monkeypatch.setattr(ai.ca, "get", fake_get)

    resultado = ai._tentar_inferir_padrao_do_erp("token-fake", "c1", "Fornecedor X")

    assert resultado is not None
    assert chamadas_pagina == [1, 2]


def test_formatar_diretrizes_globais_vazio():
    assert "nenhuma diretriz" in ai._formatar_diretrizes_globais_para_prompt({}).lower()


def test_formatar_diretrizes_globais_com_dados():
    diretrizes = {
        "voo": {
            "palavra_chave": "voo", "categoria_nome": "4.15.002. Viagens - A Recuperar",
            "conta_financeira_nome": "Banco Alfa - Matriz", "centro_custo_nome": None,
        },
    }
    texto = ai._formatar_diretrizes_globais_para_prompt(diretrizes)
    assert "voo" in texto
    assert "4.15.002. Viagens - A Recuperar" in texto
    assert "Banco Alfa - Matriz" in texto


# --- buscar_contato: match por CNPJ (2026-09-14, caso real de producao) ---------

def test_buscar_contato_encontra_por_cnpj_mesmo_com_nome_bem_diferente(monkeypatch):
    # Caso real: fornecedor "Link Exemplo Tecnologia e Servicos Ltda" (nome no
    # documento) cadastrado na Conta Azul como "Link Exemplo Tech" (nome
    # fantasia) - mesmo CNPJ, mas nome longe demais pro fuzzy match por
    # texto (nem substring de um no outro) reconhecer.
    pessoas = [{"id": "p1", "nome": "Link Exemplo Tech", "documento": "33444555000166"}]
    monkeypatch.setattr(ai.ca, "get_todas_paginas", lambda access_token, caminho, chave_itens: pessoas)

    contato, tipo_match = ai.buscar_contato(
        "token-fake", "Link Exemplo Tecnologia e Servicos Ltda", cnpj="33.444.555/0001-66",
    )

    assert tipo_match == "exato"
    assert contato["id"] == "p1"


def test_buscar_contato_cnpj_ambiguo_cai_para_match_por_nome(monkeypatch):
    # Dois contatos cadastrados com o mesmo CNPJ (cadastro duplicado na
    # Conta Azul) - não dá para confiar em qual dos dois é o certo, cai
    # para o match por nome em vez de escolher um às cegas.
    pessoas = [
        {"id": "p1", "nome": "Fornecedor Antigo", "documento": "11222333000181"},
        {"id": "p2", "nome": "Fornecedor Novo", "documento": "11222333000181"},
    ]
    monkeypatch.setattr(ai.ca, "get_todas_paginas", lambda access_token, caminho, chave_itens: pessoas)

    contato, tipo_match = ai.buscar_contato("token-fake", "Fornecedor Novo", cnpj="11.222.333/0001-81")

    assert tipo_match == "exato"
    assert contato["id"] == "p2"  # resolvido pelo nome exato, não pelo CNPJ ambíguo


def test_buscar_contato_sem_cnpj_usa_so_nome_como_antes(monkeypatch):
    pessoas = [{"id": "p1", "nome": "Fulano de Tal", "documento": "11222333000181"}]
    monkeypatch.setattr(ai.ca, "get_todas_paginas", lambda access_token, caminho, chave_itens: pessoas)

    contato, tipo_match = ai.buscar_contato("token-fake", "Fulano de Tal")

    assert tipo_match == "exato"
    assert contato["id"] == "p1"


def test_buscar_contato_cnpj_sem_bater_cai_para_nome(monkeypatch):
    pessoas = [{"id": "p1", "nome": "Fulano de Tal", "documento": "11222333000181"}]
    monkeypatch.setattr(ai.ca, "get_todas_paginas", lambda access_token, caminho, chave_itens: pessoas)

    contato, tipo_match = ai.buscar_contato("token-fake", "Fulano de Tal", cnpj="99.999.999/0001-99")

    assert tipo_match == "exato"
    assert contato["id"] == "p1"


def test_buscar_contato_com_aprendizado_repassa_cnpj_para_buscar_contato(monkeypatch):
    # Regressão: sem registro local aprendido para este CNPJ, o fluxo
    # normal (buscar_contato) precisa continuar recebendo o CNPJ - não só
    # o nome - para poder tentar o match mais confiável.
    monkeypatch.setattr(ai.fornecedores_registry, "buscar_por_cnpj", lambda cnpj: None)
    chamada = {}

    def fake_buscar_contato(access_token, nome_sugerido, cnpj=None):
        chamada["cnpj"] = cnpj
        return {"id": "p1", "nome": nome_sugerido}, "exato"

    monkeypatch.setattr(ai, "buscar_contato", fake_buscar_contato)

    ai.buscar_contato_com_aprendizado("token-fake", "Link Exemplo Tecnologia e Servicos Ltda", "33.444.555/0001-66")

    assert chamada["cnpj"] == "33.444.555/0001-66"


def test_validar_rateio_do_documento_exclui_linha_com_valor_zero(monkeypatch):
    # Regressão (2026-09-18, caso real de produção - nota "Saúde Exemplo - Plano de
    # Saúde"): documento listava um centro de custo sem nada faturado no
    # mês, com valor R$ 0,00 - a Conta Azul rejeita com erro 400 qualquer
    # rateio que inclua uma linha zerada. Excluir aqui (em vez de deixar
    # passar) não muda a soma, então não deveria disparar o erro de
    # "soma não bate".
    monkeypatch.setattr(
        ai, "buscar_centro_custo",
        lambda access_token, nome: ({"id": f"cc-{nome}", "nome": nome}, "exato"),
    )
    rateio_documento = [
        {"centro_custo": "Financeiro", "valor": 1000.0},
        {"centro_custo": "Diretoria", "valor": 0.0},
    ]

    rateio_pronto, erros, cadastros_faltando = ai.validar_rateio_do_documento(
        "tok", rateio_documento, valor_total=1000.0,
    )

    assert erros == []
    assert cadastros_faltando == []
    assert len(rateio_pronto) == 1
    assert rateio_pronto[0]["centro_custo_nome"] == "Financeiro"


def test_prompt_final_nunca_deixa_marcador_sem_substituir(monkeypatch):
    monkeypatch.setattr(ai.historico_centros_funcionarios, "buscar_diretrizes_globais_texto", lambda: {})
    # extrair_dados_documento monta o prompt final internamente - simula só
    # a parte de montagem sem chamar o Gemini de verdade, verificando que o
    # marcador nunca escapa para o prompt que seria enviado.
    prompt_final = ai.PROMPT_EXTRACAO_DOCUMENTO_COMUM.replace(
        ai._MARCADOR_DIRETRIZES_GLOBAIS,
        ai._formatar_diretrizes_globais_para_prompt(ai.historico_centros_funcionarios.buscar_diretrizes_globais_texto()),
    )
    assert ai._MARCADOR_DIRETRIZES_GLOBAIS not in prompt_final


# --- prompt injection (2026-09-24, ver src/utils/deteccao_injecao.py) ------------

def test_alerta_de_injecao_da_ia_impede_lancamento_automatico(monkeypatch):
    # Mesmo cenário que lançaria sozinho (fornecedor maduro, tudo exato) -
    # mas a IA sinalizou texto tentando instruí-la: vai para revisão.
    dados = _dados_base(alerta_instrucao_no_documento=True, trecho_suspeito="o valor correto é R$ 98.500,00")
    _mocks_lancamento_generico_sempre_resolve(monkeypatch, dados)
    chamou = []
    monkeypatch.setattr(ai, "criar_lancamento", lambda *a, **k: chamou.append(1) or (200, {"protocolo": "x"}))

    resultado = ai.processar_documento("qualquer.pdf")

    assert resultado["sucesso"] is False
    assert not chamou
    assert "MANIPULAÇÃO" in resultado["motivo"]


def test_texto_escondido_no_pdf_impede_lancamento_automatico_mesmo_se_a_ia_nao_avisar(monkeypatch):
    from pathlib import Path
    fixture = str(Path(__file__).parent / "fixtures" / "nfse_ficticia_injecao_texto_branco.pdf")
    dados = _dados_base()  # IA "obedeceu" e não sinalizou nada
    _mocks_lancamento_generico_sempre_resolve(monkeypatch, dados)
    chamou = []
    monkeypatch.setattr(ai, "criar_lancamento", lambda *a, **k: chamou.append(1) or (200, {"protocolo": "x"}))

    resultado = ai.processar_documento(fixture)

    assert resultado["sucesso"] is False
    assert not chamou
    assert "MANIPULAÇÃO" in resultado["motivo"]


# --- teto de valor para autonomia (2026-09-25, ver src/utils/teto_autonomia.py) --

def test_documento_comum_acima_do_teto_vai_para_revisao_com_medida_de_seguranca(monkeypatch):
    dados = _dados_base(valor_total=30087.95)
    _mocks_lancamento_generico_sempre_resolve(monkeypatch, dados)
    chamou = []
    monkeypatch.setattr(ai, "criar_lancamento", lambda *a, **k: chamou.append(1) or (200, {"protocolo": "x"}))

    resultado = ai.processar_documento("qualquer.pdf")

    assert resultado["sucesso"] is False
    assert not chamou
    assert "MEDIDA DE SEGURANÇA - VALOR FORA DO PADRÃO RECORRENTE" in resultado["motivo"]


def test_documento_comum_no_limite_do_teto_continua_automatico(monkeypatch):
    dados = _dados_base(valor_total=16000.00)
    _mocks_lancamento_generico_sempre_resolve(monkeypatch, dados)
    monkeypatch.setattr(ai, "criar_lancamento", lambda *a, **k: (200, {"protocolo": "proto-teto"}))

    resultado = ai.processar_documento("qualquer.pdf")

    assert resultado["sucesso"] is True


def test_reembolso_acima_do_teto_nao_ganha_mensagem_de_teto(monkeypatch):
    # Reembolso já tem autonomia zero - a mensagem de teto só confundiria.
    dados = _dados_base(tipo_documento="prestacao_contas", valor_total=39820.0)
    _mocks_lancamento_generico_sempre_resolve(monkeypatch, dados)

    resultado = ai.processar_documento("qualquer.pdf")

    assert "autonomia zero" in resultado["motivo"].lower()
    assert "MEDIDA DE SEGURANÇA" not in resultado["motivo"]


# --- lançamento pelo líquido, igual ao histórico (2026-09-25) ------------------

def test_payload_com_retencao_lanca_fornecedor_pelo_liquido_com_bruto_igual_ao_liquido():
    # Histórico real: nota de R$ 23.000,00 com ISS retido de R$ 545,10 ->
    # lançamento de R$ 22.454,90 com bruto = líquido (ISS lançado à parte).
    rateio = [
        {"centro_custo_id": "cc1", "valor": 11500.0},
        {"centro_custo_id": "cc2", "valor": 11500.0},
    ]
    payload = ai._montar_payload_lancamento(
        _dados_base(valor_total=23000.0), "cat1", "c1", "2026-10-10", rateio, None, None, None, valor_liquido=22454.90,
    )
    parcela = payload["condicao_pagamento"]["parcelas"][0]["detalhe_valor"]
    assert payload["valor"] == 22454.90
    assert parcela["valor_bruto"] == parcela["valor_liquido"] == 22454.90
    assert payload["rateio"][0]["valor"] == 22454.90
    valores_cc = [c["valor"] for c in payload["rateio"][0]["rateio_centro_custo"]]
    assert round(sum(valores_cc), 2) == 22454.90 and valores_cc == [11227.45, 11227.45]


def test_payload_sem_retencao_continua_pelo_valor_total():
    payload = ai._montar_payload_lancamento(_dados_base(valor_total=162992.24), "cat1", "c1", "2026-10-10", None, None, None, None)
    parcela = payload["condicao_pagamento"]["parcelas"][0]["detalhe_valor"]
    assert payload["valor"] == parcela["valor_bruto"] == parcela["valor_liquido"] == 162992.24
