from datetime import date

from src.agentes import agente_folha
from src.agentes.agente_folha import (
    buscar_regra_historico_por_tipo, extrair_data_do_nome_arquivo,
    processar_tabela, separar_e_ajustar_valores_negativos,
)
from src.utils.regras_folha_pagamento import REGRAS_FOLHA


def test_sem_valores_negativos_ou_zerados_retorna_lista_original():
    rateio = [
        {"centro_custo": "Comercial", "valor": 100.0},
        {"centro_custo": "Financeiro", "valor": 50.0},
    ]
    positivos, valor_total, ajustes = separar_e_ajustar_valores_negativos(rateio, 150.0)

    assert positivos == rateio
    assert valor_total == 150.0
    assert ajustes == []


def test_valor_negativo_e_removido_e_somado_ao_total():
    rateio = [
        {"centro_custo": "Comercial", "valor": 100.0},
        {"centro_custo": "Administrativo", "valor": -20.0},
    ]
    positivos, valor_total, ajustes = separar_e_ajustar_valores_negativos(rateio, 80.0)

    assert positivos == [{"centro_custo": "Comercial", "valor": 100.0}]
    assert valor_total == 100.0  # 80.0 (total original) + 20.0 (abs do negativo)
    assert len(ajustes) == 1
    assert "Administrativo" in ajustes[0]


def test_valor_zerado_e_removido_sem_alterar_total():
    rateio = [
        {"centro_custo": "Comercial", "valor": 100.0},
        {"centro_custo": "Financeiro", "valor": 0.0},
    ]
    positivos, valor_total, ajustes = separar_e_ajustar_valores_negativos(rateio, 100.0)

    assert positivos == [{"centro_custo": "Comercial", "valor": 100.0}]
    assert valor_total == 100.0
    assert ajustes == []  # zerado é removido silenciosamente, sem gerar aviso


def test_mistura_de_positivos_negativos_e_zerados():
    rateio = [
        {"centro_custo": "Comercial", "valor": 100.0},
        {"centro_custo": "Administrativo", "valor": -20.0},
        {"centro_custo": "Financeiro", "valor": -5.0},
        {"centro_custo": "TI", "valor": 0.0},
    ]
    positivos, valor_total, ajustes = separar_e_ajustar_valores_negativos(rateio, 75.0)

    assert positivos == [{"centro_custo": "Comercial", "valor": 100.0}]
    assert valor_total == 100.0  # 75.0 + 20.0 + 5.0
    assert len(ajustes) == 2


def test_processar_tabela_nao_propaga_excecao_inesperada_de_tipo_reconhecido(monkeypatch):
    # Regressão (item 3, 2026-09-02): um erro inesperado no processamento de
    # um tipo COM regra cadastrada (rede, dado faltando de um jeito não
    # previsto, etc.) não pode travar o documento inteiro - precisa virar
    # uma tabela marcada para revisão, com dados_para_edicao preenchido.
    def _explode(*args, **kwargs):
        raise RuntimeError("falha inesperada simulada")

    monkeypatch.setattr(agente_folha, "_processar_tabela_tipo_reconhecido", _explode)

    tabela = {
        "tipo": "inss",
        "circulada": True,
        "rateio": [{"centro_custo": "1122 - Administrativo", "valor": 100.0}],
        "funcionario": None,
    }
    resultado = processar_tabela(tabela, {"competencia": "01/2026"}, access_token="token-fake")

    assert resultado["sucesso"] is False
    assert "REQUER REVISÃO HUMANA" in resultado["motivo"]
    assert resultado["dados_para_edicao"]["tipo_lancamento"] == "inss"
    assert resultado["dados_para_edicao"]["valor_total"] == 100.0


def test_buscar_regra_historico_por_tipo_herda_categoria(monkeypatch):
    def fake_get(access_token, caminho, params=None):
        return {"itens": [
            {
                "descricao": "Termo de Rescisão - Fulano de Tal",
                "data_vencimento": "2026-08-15",
                "categorias": [{"id": "cat-rescisao", "nome": "4.02.006. Rescisões"}],
            },
        ]}

    monkeypatch.setattr(agente_folha.ca, "get", fake_get)

    regra, motivo = buscar_regra_historico_por_tipo("token-fake", "termo_rescisao", meses_para_tras=1)

    assert motivo is None
    assert regra["categoria"]["nome"] == "4.02.006. Rescisões"


def test_buscar_regra_historico_por_tipo_nunca_lanca_excecao_em_falha_de_rede(monkeypatch):
    def fake_get_com_erro(access_token, caminho, params=None):
        raise RuntimeError("falha de rede simulada")

    monkeypatch.setattr(agente_folha.ca, "get", fake_get_com_erro)

    # Não deve lançar - só volta (None, motivo) mesmo com a rede falhando
    # em todos os meses pesquisados.
    regra, motivo = buscar_regra_historico_por_tipo("token-fake", "tipo_qualquer", meses_para_tras=2)

    assert regra is None
    assert motivo is not None


def _bloquear_autonomia_total_rescisao(monkeypatch):
    """As 3 tabelas de teste abaixo usam access_token='token-fake' e devem
    exercer o caminho de REVISÃO MANUAL (motivo do teste) - sem isso, a
    autonomia total de rescisão (ver _tentar_lancamento_automatico_rescisao)
    tentaria uma chamada de rede real para resolver o contato. Simula
    "contato não encontrado" para garantir que a função devolve None e o
    fluxo cai no caminho de revisão manual sendo testado, sem bater na
    Conta Azul de verdade."""
    monkeypatch.setattr(
        agente_folha, "buscar_contato", lambda access_token, nome: (None, "nao_encontrado"),
    )


def test_tipo_desconhecido_rescisao_usa_fallback_quando_historico_vazio(monkeypatch):
    # Regra fixa (2026-09-02, decisão do usuário): rescisão SEMPRE usa a
    # categoria "4.02.006. Rescisões - TRCT" e a conta "Banco Alfa - EMPX
    # Matriz", nunca dependente de histórico (que nunca acharia nada de
    # qualquer forma - nome de funcionário demitido não se repete).
    _bloquear_autonomia_total_rescisao(monkeypatch)
    monkeypatch.setattr(
        agente_folha, "buscar_regra_historico_por_tipo",
        lambda access_token, tipo, meses_para_tras=12: (None, "nada encontrado"),
    )

    tabela = {
        "tipo": "termo_rescisao",
        "circulada": True,
        "rateio": [{"centro_custo": "1122", "valor": 500.0}],
        "funcionario": "FULANO DE TAL",
        "data_pagamento_documento": "10/09/2026",
    }
    resultado = processar_tabela(tabela, {"competencia": None}, access_token="token-fake")

    info = resultado["dados_para_edicao"]
    assert info["categoria_nome_sugerida"] == "4.02.006. Rescisões - TRCT"
    assert info["conta_financeira_nome_sugerida"] == "Banco Alfa - Matriz"
    assert info["fornecedor_nome_sugerido"] == "FULANO DE TAL"


def test_tipo_desconhecido_rescisao_usa_nome_do_arquivo_mesmo_com_data_no_documento(monkeypatch):
    # Regra nova (2026-09-02): para tipo "rescisão", a data do NOME DO
    # ARQUIVO tem prioridade sobre a data escrita no documento (não é mais
    # último recurso) - mesmo o documento trazendo uma data válida
    # diferente, o nome do arquivo é quem decide.
    _bloquear_autonomia_total_rescisao(monkeypatch)
    monkeypatch.setattr(
        agente_folha, "buscar_regra_historico_por_tipo",
        lambda access_token, tipo, meses_para_tras=12: (None, "nada encontrado"),
    )

    tabela = {
        "tipo": "termo_rescisao",
        "circulada": True,
        "rateio": [{"centro_custo": "1122", "valor": 500.0}],
        "funcionario": "FULANO DE TAL",
        "data_pagamento_documento": "10/09/2026",  # deve ser ignorada
    }
    resultado = processar_tabela(
        tabela, {"competencia": None}, access_token="token-fake",
        caminho_documento="/entrada/rescisao_fulano_2026-08-26.pdf",
    )

    info = resultado["dados_para_edicao"]
    assert info["data_vencimento_sugerida"] == "2026-08-26"
    assert info["data_competencia_sugerida"] == "2026-08-26"


def test_tipo_desconhecido_rescisao_cai_para_documento_se_nome_do_arquivo_nao_tiver_data(monkeypatch):
    # Mesmo com a prioridade invertida para rescisão, o documento continua
    # servindo de fallback quando o nome do arquivo não traz data nenhuma.
    _bloquear_autonomia_total_rescisao(monkeypatch)
    monkeypatch.setattr(
        agente_folha, "buscar_regra_historico_por_tipo",
        lambda access_token, tipo, meses_para_tras=12: (None, "nada encontrado"),
    )

    tabela = {
        "tipo": "termo_rescisao",
        "circulada": True,
        "rateio": [{"centro_custo": "1122", "valor": 500.0}],
        "funcionario": "FULANO DE TAL",
        "data_pagamento_documento": "10/09/2026",
    }
    resultado = processar_tabela(
        tabela, {"competencia": None}, access_token="token-fake",
        caminho_documento="/entrada/rescisao_fulano_sem_data.pdf",
    )

    info = resultado["dados_para_edicao"]
    assert info["data_vencimento_sugerida"] == "2026-09-10"


def test_tipo_desconhecido_sem_relacao_rescisao_nao_aplica_fallback(monkeypatch):
    monkeypatch.setattr(
        agente_folha, "buscar_regra_historico_por_tipo",
        lambda access_token, tipo, meses_para_tras=12: (None, "nada encontrado"),
    )

    tabela = {"tipo": "vale_transporte", "circulada": True, "rateio": [{"centro_custo": "1122", "valor": 50.0}]}
    resultado = processar_tabela(tabela, {"competencia": None}, access_token="token-fake")

    info = resultado["dados_para_edicao"]
    assert info["categoria_nome_sugerida"] is None
    assert info["conta_financeira_nome_sugerida"] is None


def test_extrair_data_do_nome_arquivo_formato_aaaa_mm_dd():
    caminho = r"C:\entrada\SALARIOS_SEMBANCO_0_2026-08-26.pdf"
    assert extrair_data_do_nome_arquivo(caminho) == date(2026, 8, 26)


def test_extrair_data_do_nome_arquivo_formato_dd_mm_aaaa():
    caminho = "/entrada/rescisao_fulano_26-08-2026.pdf"
    assert extrair_data_do_nome_arquivo(caminho) == date(2026, 8, 26)


def test_extrair_data_do_nome_arquivo_prioriza_aaaa_mm_dd_quando_ambos_presentes():
    # Formato AAAA-MM-DD é tentado primeiro - evita ambiguidade se por
    # algum motivo o nome tivesse os dois padrões.
    caminho = "documento_2026-08-26_outrascoisas.pdf"
    assert extrair_data_do_nome_arquivo(caminho) == date(2026, 8, 26)


def test_extrair_data_do_nome_arquivo_sem_data_retorna_none():
    assert extrair_data_do_nome_arquivo("SALARIOS_SEMBANCO_0.pdf") is None


def test_extrair_data_do_nome_arquivo_caminho_none_retorna_none():
    assert extrair_data_do_nome_arquivo(None) is None


def test_extrair_data_do_nome_arquivo_data_invalida_retorna_none_sem_lancar():
    # "2026-13-40" não é uma data válida (mês 13, dia 40) - não deve lançar.
    assert extrair_data_do_nome_arquivo("documento_2026-13-40.pdf") is None


def test_extrair_data_do_nome_arquivo_aceita_separador_underline():
    caminho = "2026_07_20_FGTS_FICTICIO_pag3_R144.32139.pdf"
    assert extrair_data_do_nome_arquivo(caminho) == date(2026, 7, 20)


def test_extrair_data_do_nome_arquivo_aceita_separador_espaco():
    caminho = "2026 08 31_IRPJ_3 TRIM.2026_EMPX_R$ 1.234.567,89.pdf"
    assert extrair_data_do_nome_arquivo(caminho) == date(2026, 8, 31)


def test_extrair_data_do_nome_arquivo_nao_mistura_separadores_diferentes():
    # "2026-08_26" tem um "-" e um "_" na mesma data - não deve casar.
    assert extrair_data_do_nome_arquivo("documento_2026-08_26.pdf") is None


def test_extrair_data_do_nome_arquivo_formato_americano_quando_dd_mm_invalido():
    # "08-25-2026": lido como DD-MM-AAAA daria mês 25 (inválido) - cai para
    # MM-DD-AAAA (formato americano), dia 25, mês 08.
    caminho = "invoice_08-25-2026.pdf"
    assert extrair_data_do_nome_arquivo(caminho) == date(2026, 8, 25)


def test_extrair_data_do_nome_arquivo_ambiguo_assume_formato_brasileiro():
    # "05-08-2026": os dois números são <=12, ambiguidade real - assume-se
    # DD-MM-AAAA (padrão brasileiro) por padrão.
    caminho = "documento_05-08-2026.pdf"
    assert extrair_data_do_nome_arquivo(caminho) == date(2026, 8, 5)


def test_processar_tabela_salarios_sem_competencia_usa_data_do_nome_do_arquivo(monkeypatch):
    # Regressão (caso real de produção): "salarios" sem competência da
    # empresa toda (documento é na verdade um Termo de Rescisão individual)
    # deve usar a data do nome do arquivo diretamente como vencimento e
    # competência, sem aplicar ultimo_dia_util_do_mes/dia_20_mes_seguinte.
    def fake_buscar_contato(access_token, nome):
        return {"id": "contato-1", "nome": nome}, "exato"

    def fake_buscar_categoria(access_token, nome):
        return {"id": "cat-1", "nome": nome}, "exato"

    def fake_buscar_conta_financeira(access_token, nome):
        return {"id": "conta-1", "nome": nome}, "exato"

    def fake_buscar_centro_custo(access_token, nome):
        return {"id": "centro-1", "nome": nome}, "exato"

    def fake_criar_lancamento(access_token, dados, categoria_id, contato_id, data_vencimento, **kwargs):
        return 200, {"protocolo": "proto-teste"}

    monkeypatch.setattr(agente_folha, "buscar_contato", fake_buscar_contato)
    monkeypatch.setattr(agente_folha, "buscar_categoria", fake_buscar_categoria)
    monkeypatch.setattr(agente_folha, "buscar_conta_financeira", fake_buscar_conta_financeira)
    monkeypatch.setattr(agente_folha, "buscar_centro_custo", fake_buscar_centro_custo)
    monkeypatch.setattr(agente_folha, "criar_lancamento", fake_criar_lancamento)
    monkeypatch.setattr(
        agente_folha.verificacao_duplicidade, "verificar_lancamento_similar_existe",
        lambda *args, **kwargs: None,
    )

    tabela = {
        "tipo": "salarios",
        "arquivo_bancario": "Alfa",
        "circulada": True,
        "rateio": [{"centro_custo": "1122 - Administrativo", "valor": 1000.0}],
        "funcionario": "FULANO DE TAL",
    }
    dados_folha = {"competencia": None}
    resultado = processar_tabela(
        tabela, dados_folha, access_token="token-fake",
        caminho_documento="SALARIOS_SEMBANCO_0_2026-08-26.pdf",
    )

    assert resultado["sucesso"] is True
    assert resultado["detalhes"]["data_vencimento"] == "2026-08-26"
    assert resultado["detalhes"]["data_competencia"] == "2026-08-26"


def test_regra_grrf_usa_valores_fixados_e_nunca_calcula_por_formula():
    # Regressão (2026-09-02, caso real de produção): GRRF é FGTS de
    # RESCISÃO, tem data exata própria no documento - nunca pode cair na
    # fórmula de "mês cheio" (ultimo_dia_util_do_mes/dia_20_mes_seguinte)
    # usada pelo FGTS comum.
    regra_grrf = REGRAS_FOLHA["grrf"]
    assert regra_grrf["categoria_nome"] == "4.02.009. FGTS s/ Rescisão"
    assert regra_grrf["descricao_template"] == "FGTS Rescisão {funcionario}"
    assert regra_grrf["data_competencia_func"] is None
    assert regra_grrf["vencimento_func"] is None


def test_grrf_interceptado_quando_tipo_fgts_mas_contexto_menciona_rescisao():
    # Trava de segurança (2026-09-02, caso real de produção): a IA
    # classificou por engano um FGTS de rescisão como "fgts" comum - o
    # texto bruto extraído junto da tabela ainda menciona "GRRF", então o
    # código deve forçar o tipo para "grrf" antes de processar. Sem
    # funcionário, a tabela cai cedo (sem chamar a rede) já usando a regra
    # de "grrf" - dá pra confirmar isso comparando a categoria sugerida.
    tabela = {
        "tipo": "fgts",
        "contexto_bruto": "GRRF - Guia de Recolhimento Rescisório do FGTS",
        "circulada": True,
        "rateio": [{"centro_custo": "1122", "valor": 500.0}],
        "funcionario": None,
    }
    resultado = processar_tabela(tabela, {"competencia": None}, access_token="token-fake")

    info = resultado["dados_para_edicao"]
    assert info["tipo_lancamento"] == "grrf"
    assert info["categoria_nome_sugerida"] == REGRAS_FOLHA["grrf"]["categoria_nome"]
    assert info["categoria_nome_sugerida"] != REGRAS_FOLHA["fgts"]["categoria_nome"]


def test_fgts_comum_nao_e_afetado_quando_contexto_nao_menciona_rescisao():
    # A trava só deve disparar com um dos termos específicos - um FGTS
    # comum de verdade (contexto sem menção a rescisão) continua sendo
    # tratado como "fgts", com sua própria fórmula/categoria.
    tabela = {
        "tipo": "fgts",
        "contexto_bruto": "FGTS TOTAL - Arquivo Bancário Banco Alfa",
        "circulada": True,
        "rateio": [{"centro_custo": "1122", "valor": 500.0}],
        "funcionario": None,
    }
    resultado = processar_tabela(tabela, {"competencia": "08/2026"}, access_token="token-fake")

    assert resultado["detalhes"].get("tipo_nao_reconhecido") is None
    # Tipo "fgts" é reconhecido (está em REGRAS_FOLHA) - não cai no caminho
    # de tipo desconhecido, então "dados_para_edicao" só aparece se algo
    # mais adiante falhar (ex: contato via rede); o importante aqui é que
    # NÃO foi redirecionado para "grrf".
    assert resultado.get("dados_para_edicao", {}).get("tipo_lancamento") != "grrf"


def test_grrf_usa_data_do_nome_arquivo_quando_documento_nao_traz_data():
    # Trava de GRRF (2026-09-02): quando o campo "Pagar este documento até"
    # não veio no documento, usa a data do nome do arquivo em vez de
    # deixar a data em branco (nunca cai pra fórmula de mês cheio).
    tabela = {
        "tipo": "fgts",
        "contexto_bruto": "GRRF",
        "circulada": True,
        "rateio": [{"centro_custo": "1122", "valor": 500.0}],
        "funcionario": None,
        "data_pagamento_documento": None,
    }
    resultado = processar_tabela(
        tabela, {"competencia": None}, access_token="token-fake",
        caminho_documento="/entrada/grrf_fulano_2026-09-04.pdf",
    )

    info = resultado["dados_para_edicao"]
    assert info["tipo_lancamento"] == "grrf"
    assert info["data_vencimento_sugerida"] == "2026-09-04"
    assert info["data_competencia_sugerida"] == "2026-09-04"


def test_lancamento_folha_reconhecido_nunca_preenche_observacao(monkeypatch):
    # Pedido explícito do usuário (2026-09-02): campo de observação/nota
    # estritamente vazio em todo lançamento de folha - nem o texto padrão
    # ("Lançamento criado automaticamente...") nem "Enviado por: fulano".
    dados_capturados = {}

    def fake_criar_lancamento(access_token, dados, categoria_id, contato_id, data_vencimento, **kwargs):
        dados_capturados.update(dados)
        return 200, {"protocolo": "proto-teste"}

    monkeypatch.setattr(agente_folha, "buscar_contato", lambda access_token, nome: ({"id": "c1", "nome": nome}, "exato"))
    monkeypatch.setattr(agente_folha, "buscar_categoria", lambda access_token, nome: ({"id": "cat1", "nome": nome}, "exato"))
    monkeypatch.setattr(agente_folha, "buscar_conta_financeira", lambda access_token, nome: ({"id": "conta1", "nome": nome}, "exato"))
    monkeypatch.setattr(agente_folha, "buscar_centro_custo", lambda access_token, nome: ({"id": "centro1", "nome": nome}, "exato"))
    monkeypatch.setattr(agente_folha, "criar_lancamento", fake_criar_lancamento)
    monkeypatch.setattr(
        agente_folha.verificacao_duplicidade, "verificar_lancamento_similar_existe",
        lambda *args, **kwargs: None,
    )

    tabela = {
        "tipo": "salarios",
        "arquivo_bancario": "Alfa",
        "circulada": True,
        "rateio": [{"centro_custo": "1122 - Administrativo", "valor": 1000.0}],
        "funcionario": "FULANO DE TAL",
    }
    resultado = processar_tabela(
        tabela, {"competencia": None}, access_token="token-fake", usuario="alguem",
        caminho_documento="SALARIOS_SEMBANCO_0_2026-08-26.pdf",
    )

    assert resultado["sucesso"] is True
    assert dados_capturados["observacoes"] == ""


def test_padrao_documento_para_maturacao_agrupa_variacoes_de_rescisao():
    # Pequenas diferenças de fraseado que a IA pode gerar para o mesmo tipo
    # de situação não podem fragmentar a contagem de maturação.
    assert agente_folha.padrao_documento_para_maturacao("termo_rescisao") == "rescisao"
    assert agente_folha.padrao_documento_para_maturacao("rescisao_contrato_trabalho") == "rescisao"
    assert agente_folha.padrao_documento_para_maturacao("RESCISÃO") == "rescisao"
    assert agente_folha.padrao_documento_para_maturacao("fgts") == "fgts"


def test_documento_maduro_para_autonomia_respeita_minimo(monkeypatch):
    monkeypatch.setattr(agente_folha.historico_centros_funcionarios, "contagem_aprovacoes_padrao", lambda padrao: 2)
    assert agente_folha.documento_maduro_para_autonomia("termo_rescisao") is False

    monkeypatch.setattr(agente_folha.historico_centros_funcionarios, "contagem_aprovacoes_padrao", lambda padrao: 3)
    assert agente_folha.documento_maduro_para_autonomia("termo_rescisao") is True


def test_registrar_aprovacao_manual_padrao_grava_sob_padrao_canonico(tmp_path, monkeypatch):
    monkeypatch.setattr(
        agente_folha.historico_centros_funcionarios, "CAMINHO_REGISTRO", tmp_path / "historico_centros_funcionarios.json",
    )

    contagem = agente_folha.registrar_aprovacao_manual_padrao("termo_rescisao", "Conferir data do TRCT")

    assert contagem == 1
    # Gravado sob o padrão canônico "rescisao", não sob o "tipo" literal
    # que a IA extraiu - é o que faz a contagem valer pra qualquer variação.
    entrada = agente_folha.historico_centros_funcionarios.buscar_padrao_documento("rescisao")
    assert entrada["contagem_aprovacoes"] == 1
    assert entrada["notas_diretriz_humana"][0]["texto"] == "Conferir data do TRCT"


def _mocks_autonomia_total_rescisao_sempre_resolve(monkeypatch):
    """Mocka toda a cadeia (contato, categoria, conta financeira, centro de
    custo, duplicidade, criação) para sempre resolver com sucesso - usado
    pelos testes de maturação abaixo para isolar SÓ o efeito do contador,
    sem depender de as outras peças resolverem por si (já cobertas nos
    testes de autonomia total de rescisão mais acima)."""
    monkeypatch.setattr(agente_folha, "buscar_contato", lambda access_token, nome: ({"id": "c1", "nome": nome}, "exato"))
    monkeypatch.setattr(agente_folha, "buscar_categoria", lambda access_token, nome: ({"id": "cat1", "nome": nome}, "exato"))
    monkeypatch.setattr(agente_folha, "buscar_conta_financeira", lambda access_token, nome: ({"id": "conta1", "nome": nome}, "exato"))
    monkeypatch.setattr(agente_folha, "buscar_centro_custo", lambda access_token, nome: ({"id": "centro1", "nome": nome}, "exato"))
    monkeypatch.setattr(
        agente_folha.verificacao_duplicidade, "verificar_lancamento_similar_existe",
        lambda *args, **kwargs: None,
    )


def _tabela_rescisao_completa():
    return {
        "tipo": "termo_rescisao",
        "circulada": True,
        "rateio": [{"centro_custo": "1122", "valor": 500.0}],
        "funcionario": "FULANO DE TAL",
    }


def test_autonomia_total_rescisao_nao_dispara_quando_padrao_imaturo(monkeypatch):
    # Autonomia condicional (2026-09-02, pedido explícito do usuário): com
    # TUDO resolvendo sozinho, mas o padrão "rescisao" com menos de 3
    # aprovações humanas registradas, o lançamento NUNCA pode sair
    # automático - precisa cair em revisão manual mesmo assim.
    _mocks_autonomia_total_rescisao_sempre_resolve(monkeypatch)
    monkeypatch.setattr(agente_folha.historico_centros_funcionarios, "contagem_aprovacoes_padrao", lambda padrao: 2)

    chamou_criar_lancamento = []
    monkeypatch.setattr(
        agente_folha, "criar_lancamento",
        lambda *a, **k: (chamou_criar_lancamento.append(1) or (200, {"protocolo": "nao-deveria-lancar"})),
    )

    resultado = processar_tabela(
        _tabela_rescisao_completa(), {"competencia": None}, access_token="token-fake",
        caminho_documento="/entrada/rescisao_fulano_2026-08-26.pdf",
    )

    assert resultado["sucesso"] is False
    assert "dados_para_edicao" in resultado
    assert not chamou_criar_lancamento


def test_autonomia_total_rescisao_dispara_quando_padrao_maduro(monkeypatch):
    # Mesmo cenário acima, mas com o padrão já maduro (>= 3 aprovações) -
    # agora sim lança 100% automático.
    _mocks_autonomia_total_rescisao_sempre_resolve(monkeypatch)
    monkeypatch.setattr(agente_folha.historico_centros_funcionarios, "contagem_aprovacoes_padrao", lambda padrao: 3)
    monkeypatch.setattr(agente_folha, "criar_lancamento", lambda *a, **k: (200, {"protocolo": "proto-maduro"}))

    resultado = processar_tabela(
        _tabela_rescisao_completa(), {"competencia": None}, access_token="token-fake",
        caminho_documento="/entrada/rescisao_fulano_2026-08-26.pdf",
    )

    assert resultado["sucesso"] is True
    assert resultado["detalhes"]["lancado_automaticamente_sem_revisao"] is True


def test_tabela_ignorada_propositalmente_vem_com_dados_para_edicao_preenchidos():
    # Regressão (2026-09-03, falso positivo real de produção): antes, uma
    # tabela não circulada ("ignorada de propósito") devolvia SEM
    # dados_para_edicao - se um humano precisasse forçar o lançamento
    # manual (documento legítimo que a IA não detectou a marcação visual),
    # o formulário no painel vinha totalmente em branco. Agora vem
    # pré-preenchido com o que já foi extraído.
    tabela = {
        "tipo": "inss",
        "circulada": False,
        "rateio": [{"centro_custo": "1122", "valor": 300.0}],
    }
    resultado = processar_tabela(tabela, {"competencia": "08/2026"}, access_token="token-fake")

    assert resultado["sucesso"] is False
    assert resultado["ignorado_propositalmente"] is True
    info = resultado["dados_para_edicao"]
    assert info["tipo_lancamento"] == "inss"
    assert info["rateio_doc"] == [{"centro_custo": "1122", "valor": 300.0}]
    assert info["valor_total"] == 300.0
    # "inss" é uma regra conhecida (REGRAS_FOLHA) - a sugestão de categoria
    # vem preenchida mesmo a tabela estando ignorada.
    assert info["categoria_nome_sugerida"] == REGRAS_FOLHA["inss"]["categoria_nome"]


def test_tabela_ignorada_propositalmente_usa_data_do_nome_do_arquivo_como_sugestao():
    tabela = {
        "tipo": "ferias",
        "circulada": False,
        "rateio": [{"centro_custo": "A_DEFINIR", "valor": 500.0}],
        "funcionario": "FULANO DE TAL",
    }
    resultado = processar_tabela(
        tabela, {"competencia": None}, access_token="token-fake",
        caminho_documento="/entrada/ferias_fulano_2026-08-26.pdf",
    )

    info = resultado["dados_para_edicao"]
    assert info["data_vencimento_sugerida"] == "2026-08-26"
    assert info["data_competencia_sugerida"] == "2026-08-26"
    assert info["fornecedor_nome_sugerido"] == "FULANO DE TAL"


def _mocks_lancamento_folha_sempre_resolve(monkeypatch):
    """Mocka toda a cadeia (contato, categoria, conta financeira, centro de
    custo, duplicidade) para sempre resolver com sucesso - usado pelos
    testes de maturação de tipos reconhecidos abaixo (grrf/ferias/
    pro_labore/fgts), para isolar só o efeito do contador."""
    monkeypatch.setattr(agente_folha, "buscar_contato", lambda access_token, nome: ({"id": "c1", "nome": nome}, "exato"))
    monkeypatch.setattr(agente_folha, "buscar_categoria", lambda access_token, nome: ({"id": "cat1", "nome": nome}, "exato"))
    monkeypatch.setattr(agente_folha, "buscar_conta_financeira", lambda access_token, nome: ({"id": "conta1", "nome": nome}, "exato"))
    monkeypatch.setattr(agente_folha, "buscar_centro_custo", lambda access_token, nome: ({"id": "centro1", "nome": nome}, "exato"))
    monkeypatch.setattr(
        agente_folha.verificacao_duplicidade, "verificar_lancamento_similar_existe",
        lambda *args, **kwargs: None,
    )


def _tabela_ferias_completa():
    return {
        "tipo": "ferias",
        "circulada": True,
        "rateio": [{"centro_custo": "1122 - Administrativo", "valor": 1000.0}],
        "funcionario": "FULANO DE TAL",
        "data_pagamento_documento": "10/09/2026",
        "periodo_gozo": "10/09/2026 A 24/09/2026 = 15 Dias",
    }


def test_ferias_nao_lanca_automatico_quando_padrao_imaturo(monkeypatch):
    # Regressão (2026-09-03, pedido explícito do usuário - "Inconsistência
    # de Proteção" apontada em auditoria): documentos individuais
    # RECONHECIDOS (ferias/pro_labore/grrf, fornecedor_nome=None na regra)
    # ganharam a mesma proteção de maturação que rescisão - com TUDO
    # resolvendo sozinho, mas o padrão "ferias" com menos de 3 aprovações
    # humanas, o lançamento NUNCA pode sair automático.
    _mocks_lancamento_folha_sempre_resolve(monkeypatch)
    monkeypatch.setattr(agente_folha.historico_centros_funcionarios, "contagem_aprovacoes_padrao", lambda padrao: 2)

    chamou_criar_lancamento = []
    monkeypatch.setattr(
        agente_folha, "criar_lancamento",
        lambda *a, **k: (chamou_criar_lancamento.append(1) or (200, {"protocolo": "nao-deveria-lancar"})),
    )

    resultado = processar_tabela(_tabela_ferias_completa(), {"competencia": None}, access_token="token-fake")

    assert resultado["sucesso"] is False
    assert "dados_para_edicao" in resultado
    assert resultado["dados_para_edicao"]["tipo_lancamento"] == "ferias"
    assert not chamou_criar_lancamento


def test_ferias_lanca_automatico_quando_padrao_maduro(monkeypatch):
    # Mesmo cenário acima, mas com o padrão já maduro (>= 3 aprovações) -
    # agora sim lança 100% automático, igual sempre lançou antes desta
    # correção (comportamento preservado quando maduro).
    _mocks_lancamento_folha_sempre_resolve(monkeypatch)
    monkeypatch.setattr(agente_folha.historico_centros_funcionarios, "contagem_aprovacoes_padrao", lambda padrao: 3)
    monkeypatch.setattr(agente_folha, "criar_lancamento", lambda *a, **k: (200, {"protocolo": "proto-maduro"}))

    resultado = processar_tabela(_tabela_ferias_completa(), {"competencia": None}, access_token="token-fake")

    assert resultado["sucesso"] is True
    assert resultado["detalhes"]["protocolo"] == "proto-maduro"


def test_fgts_lanca_automatico_mesmo_com_padrao_imaturo(monkeypatch):
    # Tipos com fórmula fixa da empresa toda (fornecedor_nome PREENCHIDO na
    # regra, ex: fgts/inss/salarios) NÃO são afetados pela maturação -
    # continuam lançando automaticamente como sempre, mesmo com 0
    # aprovações registradas (a regra já é fixa/validada, não é uma
    # classificação ad-hoc por documento individual).
    _mocks_lancamento_folha_sempre_resolve(monkeypatch)
    monkeypatch.setattr(agente_folha.historico_centros_funcionarios, "contagem_aprovacoes_padrao", lambda padrao: 0)
    monkeypatch.setattr(agente_folha, "criar_lancamento", lambda *a, **k: (200, {"protocolo": "proto-fgts"}))

    tabela = {
        "tipo": "fgts",
        "circulada": True,
        "rateio": [{"centro_custo": "1122 - Administrativo", "valor": 1000.0}],
    }
    resultado = processar_tabela(tabela, {"competencia": "08/2026"}, access_token="token-fake")

    assert resultado["sucesso"] is True
    assert resultado["detalhes"]["protocolo"] == "proto-fgts"
