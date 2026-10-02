import json
from datetime import date

import pytest

from src.agentes import agente_nfs_e
from src.utils import regras_nfse


def _dados_base(**overrides):
    dados = {
        "numero_nota": "123",
        "tipo_servico": "Consultoria",
        "discriminacao_servicos": "Consultoria em engenharia de planejamento",
        "codigo_servico": "07.05",
        "descricao_codigo_servico": "Engenharia, agronomia, arquitetura",
        "fornecedor_nome": "Fornecedor Teste Ltda",
        "fornecedor_cnpj": "12.345.678/0001-90",
        "categoria_sugerida": "Serviços de Consultoria",
        "valor_total": 1000.0,
        "valor_imposto": None,
        "valor_liquido": None,
        "tem_boleto": False,
        "boleto_data_vencimento": None,
        "eh_prestacao_contas_de_funcionario": False,
        "rateio_centro_custo_documento": [],
    }
    dados.update(overrides)
    return dados


# --- resolver_banco_e_vencimento -------------------------------------------------

def test_resolver_banco_com_boleto_usa_alfa_e_data_do_boleto():
    info = agente_nfs_e.resolver_banco_e_vencimento(
        access_token="tok", fornecedor_cnpj="12345678000190", contato=None,
        tipo_match_contato="nao_encontrado", tem_boleto=True,
        boleto_data_vencimento="15/10/2026", data_competencia=date(2026, 10, 1),
    )
    assert info["conta_financeira_id"] == regras_nfse.CONTA_FINANCEIRA_ALFA_ID
    assert info["forma_pagamento"] == "boleto bancário"
    assert info["data_vencimento"] == date(2026, 10, 15)
    assert info["origem_banco"] == "boleto"
    assert info["permite_autonomia_banco"] is True


def test_resolver_banco_com_boleto_em_iso_resolve_igual_ao_formato_br():
    # 2026-09-14 (pedido explícito do usuário): a resposta ISO da IA
    # deixou de ser tratada como formato inválido - antes este mesmo
    # boleto caía em "boleto_data_invalida" e perdia a autonomia.
    info = agente_nfs_e.resolver_banco_e_vencimento(
        access_token="tok", fornecedor_cnpj="12345678000190", contato=None,
        tipo_match_contato="nao_encontrado", tem_boleto=True,
        boleto_data_vencimento="2026-10-15", data_competencia=date(2026, 10, 1),
    )
    assert info["data_vencimento"] == date(2026, 10, 15)
    assert info["origem_banco"] == "boleto"
    assert info["permite_autonomia_banco"] is True


def test_resolver_banco_com_boleto_data_invalida_vai_para_revisao():
    info = agente_nfs_e.resolver_banco_e_vencimento(
        access_token="tok", fornecedor_cnpj="12345678000190", contato=None,
        tipo_match_contato="nao_encontrado", tem_boleto=True,
        boleto_data_vencimento="31/02/2026",  # dia inexistente - ilegível de verdade
        data_competencia=date(2026, 10, 1),
    )
    assert info["conta_financeira_id"] is None
    assert info["origem_banco"] == "boleto_data_invalida"
    assert info["permite_autonomia_banco"] is False


def test_resolver_banco_sem_boleto_camada1_registro_local(monkeypatch):
    monkeypatch.setattr(
        agente_nfs_e.fornecedores_registry, "buscar_por_cnpj",
        lambda cnpj: {"banco_pagamento_sem_boleto": "beta", "usos_autonomos_desde_confirmacao": 0},
    )

    info = agente_nfs_e.resolver_banco_e_vencimento(
        access_token="tok", fornecedor_cnpj="12345678000190", contato=None,
        tipo_match_contato="nao_encontrado", tem_boleto=False,
        boleto_data_vencimento=None, data_competencia=date(2026, 10, 1),
    )

    assert info["conta_financeira_id"] == regras_nfse.CONTA_FINANCEIRA_BETA_ID
    assert info["origem_banco"] == "camada1_registro_local"
    assert info["permite_autonomia_banco"] is True


def test_resolver_banco_camada1_precisa_reconfirmar_apos_limite_de_usos(monkeypatch):
    monkeypatch.setattr(
        agente_nfs_e.fornecedores_registry, "buscar_por_cnpj",
        lambda cnpj: {
            "banco_pagamento_sem_boleto": "beta",
            "usos_autonomos_desde_confirmacao": regras_nfse.LIMITE_USOS_AUTONOMOS_BANCO_SEM_RECONFIRMAR,
        },
    )

    info = agente_nfs_e.resolver_banco_e_vencimento(
        access_token="tok", fornecedor_cnpj="12345678000190", contato=None,
        tipo_match_contato="nao_encontrado", tem_boleto=False,
        boleto_data_vencimento=None, data_competencia=date(2026, 10, 1),
    )

    # Ainda sugere o banco (pré-preenche o formulário), mas NÃO libera
    # autonomia total - precisa de 1 reconfirmação humana pra renovar.
    assert info["conta_financeira_id"] == regras_nfse.CONTA_FINANCEIRA_BETA_ID
    assert info["origem_banco"] == "camada1_precisa_reconfirmar"
    assert info["permite_autonomia_banco"] is False


def test_resolver_banco_sem_boleto_camada2_inferido_do_erp(monkeypatch):
    monkeypatch.setattr(agente_nfs_e.fornecedores_registry, "buscar_por_cnpj", lambda cnpj: None)
    monkeypatch.setattr(
        agente_nfs_e, "_tentar_inferir_banco_do_erp",
        lambda access_token, contato_id, nome: {
            "banco": "alfa", "conta_financeira_id": regras_nfse.CONTA_FINANCEIRA_ALFA_ID, "evidencia_qtd": 3,
        },
    )
    gravado = {}
    monkeypatch.setattr(
        agente_nfs_e.fornecedores_registry, "registrar_banco_pagamento_sem_boleto",
        lambda cnpj, banco, origem: gravado.update(cnpj=cnpj, banco=banco, origem=origem),
    )

    info = agente_nfs_e.resolver_banco_e_vencimento(
        access_token="tok", fornecedor_cnpj="12345678000190", contato={"id": "c1", "nome": "Fornecedor"},
        tipo_match_contato="exato", tem_boleto=False,
        boleto_data_vencimento=None, data_competencia=date(2026, 10, 1),
    )

    assert info["conta_financeira_id"] == regras_nfse.CONTA_FINANCEIRA_ALFA_ID
    assert info["origem_banco"] == "camada2_erp_inferido"
    assert info["permite_autonomia_banco"] is True
    assert gravado == {"cnpj": "12345678000190", "banco": "alfa", "origem": "inferido_do_erp"}


def test_resolver_banco_sem_boleto_camada3_sem_padrao(monkeypatch):
    monkeypatch.setattr(agente_nfs_e.fornecedores_registry, "buscar_por_cnpj", lambda cnpj: None)
    monkeypatch.setattr(agente_nfs_e, "_tentar_inferir_banco_do_erp", lambda *a, **k: None)

    info = agente_nfs_e.resolver_banco_e_vencimento(
        access_token="tok", fornecedor_cnpj="12345678000190", contato={"id": "c1", "nome": "Fornecedor"},
        tipo_match_contato="exato", tem_boleto=False,
        boleto_data_vencimento=None, data_competencia=date(2026, 10, 1),
    )

    assert info["conta_financeira_id"] is None
    assert info["origem_banco"] == "camada3_fallback"
    assert info["permite_autonomia_banco"] is False


def test_resolver_banco_sem_boleto_nunca_tenta_camada2_com_match_aproximado(monkeypatch):
    monkeypatch.setattr(agente_nfs_e.fornecedores_registry, "buscar_por_cnpj", lambda cnpj: None)

    def _deve_nao_ser_chamada(*a, **k):
        raise AssertionError("Camada 2 não deve ser tentada sem match EXATO de contato")

    monkeypatch.setattr(agente_nfs_e, "_tentar_inferir_banco_do_erp", _deve_nao_ser_chamada)

    info = agente_nfs_e.resolver_banco_e_vencimento(
        access_token="tok", fornecedor_cnpj="12345678000190", contato={"id": "c1", "nome": "Fornecedor"},
        tipo_match_contato="aproximado", tem_boleto=False,
        boleto_data_vencimento=None, data_competencia=date(2026, 10, 1),
    )

    assert info["conta_financeira_id"] is None
    assert info["origem_banco"] == "camada3_fallback"


def test__tentar_inferir_banco_do_erp_ignora_lancamentos_nao_recebidos(monkeypatch):
    # Regressão (2026-09-08, achado em revisão pós-implementação): sem o
    # filtro RECEBIDO, um lançamento agendado/cancelado contava como
    # evidência de banco.
    def _get_falso(access_token, caminho, params):
        pagina = params["pagina"]
        if pagina > 1:
            return {"itens": []}
        return {
            "itens": [
                {
                    "fornecedor": {"id": "c1"}, "status_traduzido": "PENDENTE",
                    "data_competencia": "2026-10-01", "data_vencimento": "2026-10-08",
                },
                {
                    "fornecedor": {"id": "c1"}, "status_traduzido": "PENDENTE",
                    "data_competencia": "2026-10-01", "data_vencimento": "2026-10-08",
                },
                {
                    "fornecedor": {"id": "c1"}, "status_traduzido": "PENDENTE",
                    "data_competencia": "2026-10-01", "data_vencimento": "2026-10-08",
                },
            ]
        }

    monkeypatch.setattr(agente_nfs_e.ca, "get", _get_falso)

    resultado = agente_nfs_e._tentar_inferir_banco_do_erp("tok", "c1", "Fornecedor Teste")

    assert resultado is None


# --- _converter_data_br -----------------------------------------------------------

@pytest.mark.parametrize("valor", ["15/10/2026", "2026-10-15", "2026/10/15"])
def test_converter_data_br_aceita_as_duas_ordens(valor):
    # 2026-09-14 (pedido explícito do usuário): a resposta ISO da IA era
    # rejeitada e mandava um boleto legível para revisão manual à toa.
    assert agente_nfs_e._converter_data_br(valor) == date(2026, 10, 15)


@pytest.mark.parametrize("valor", ["10/11", "31/02/2026", "a combinar", ""])
def test_converter_data_br_formato_invalido_levanta_erro_claro(valor):
    # A trava continua valendo para o ambíguo/inválido: esta data vira
    # vencimento lançado na Conta Azul.
    with pytest.raises(ValueError, match="formato inesperado"):
        agente_nfs_e._converter_data_br(valor)


# --- extrair_dados_nfse: distinção entre "não é NFS-e" e "extração incompleta" ----

class _RespostaFalsa:
    def __init__(self, texto):
        self.text = texto


def _mockar_gemini(monkeypatch, payload: dict):
    class _ModelosFalsos:
        def generate_content(self, model, contents):
            return _RespostaFalsa(json.dumps(payload))

    class _ClienteFalso:
        models = _ModelosFalsos()

    monkeypatch.setattr(agente_nfs_e, "_cliente_gemini", lambda: _ClienteFalso())
    monkeypatch.setattr(agente_nfs_e.gemini_inline_upload, "preparar_parte_inline", lambda caminho: object())


def test_extrair_dados_nfse_com_boleto_sem_data_levanta_excecao_especifica(monkeypatch):
    payload = _dados_base(tem_boleto=True, boleto_data_vencimento=None)
    _mockar_gemini(monkeypatch, payload)

    with pytest.raises(agente_nfs_e.NFSeExtracaoIncompleta) as exc_info:
        agente_nfs_e.extrair_dados_nfse("nota.pdf", max_tentativas=1)

    assert exc_info.value.dados_brutos["numero_nota"] == "123"


def test_extrair_dados_nfse_sem_campos_de_identidade_levanta_validation_error_comum(monkeypatch):
    from pydantic import ValidationError

    payload = _dados_base()
    del payload["numero_nota"]  # campo de identidade faltando - não parece NFS-e de jeito nenhum
    _mockar_gemini(monkeypatch, payload)

    with pytest.raises(ValidationError):
        agente_nfs_e.extrair_dados_nfse("qualquer.pdf", max_tentativas=1)


def test_extrair_dados_nfse_sucesso(monkeypatch):
    payload = _dados_base()
    _mockar_gemini(monkeypatch, payload)

    dados = agente_nfs_e.extrair_dados_nfse("nota.pdf", max_tentativas=1)

    assert dados["numero_nota"] == "123"


# --- processar_dados_nfse ---------------------------------------------------------

def _mockar_dependencias_comuns(
    monkeypatch, *, categoria_match="exato", contato_match="exato",
    registro_fornecedor=None, padrao_erp_categoria="achou", padrao_maduro=True,
    info_banco=None, duplicata=None, status_lancamento=200,
    status_importacao="nao_encontrado", item_importado=None,
    tem_data_competencia=True,
):
    """`registro_fornecedor`: quando None, simula Camada 2/3 de categoria.
    `padrao_erp_categoria`: "achou" (Camada 2 concede autonomia) ou None
    (cai na Camada 3, sem autonomia) - só relevante quando
    registro_fornecedor é None. `status_importacao`: por padrão simula que
    não há nenhum lançamento parecido já existente na Conta Azul -
    "exato"/"parcial" testados à parte (ver comentário em
    processar_dados_nfse: desde 2026-09-18, esses dois só entram como aviso
    de possível duplicata em motivos_pendentes, não bloqueiam mais a
    criação/o funil - só o "exato" COM banco resolvido continua sem criar
    do zero, completando via PATCH). `tem_data_competencia=False`: simula
    nome de arquivo sem data reconhecível (sem isso, a checagem de
    duplicata por número de nota nem é tentada)."""
    data_competencia_falsa = date(2026, 10, 1) if tem_data_competencia else None
    monkeypatch.setattr(agente_nfs_e, "extrair_data_do_nome_arquivo", lambda caminho: data_competencia_falsa)
    monkeypatch.setattr(agente_nfs_e, "obter_access_token", lambda: "tok")
    monkeypatch.setattr(
        agente_nfs_e, "buscar_lancamento_ja_importado",
        lambda *a, **k: (status_importacao, item_importado),
    )
    monkeypatch.setattr(
        agente_nfs_e, "buscar_contato_com_aprendizado",
        lambda access_token, nome, cnpj: ({"id": "contato-1", "nome": nome}, contato_match),
    )
    monkeypatch.setattr(agente_nfs_e.fornecedores_registry, "buscar_por_cnpj", lambda cnpj: registro_fornecedor)
    monkeypatch.setattr(
        agente_nfs_e, "buscar_categoria",
        lambda access_token, nome: ({"id": "cat-1", "nome": nome}, categoria_match),
    )
    monkeypatch.setattr(agente_nfs_e.historico_centros_funcionarios, "padrao_esta_maduro", lambda padrao: padrao_maduro)
    monkeypatch.setattr(agente_nfs_e.historico_centros_funcionarios, "contagem_aprovacoes_padrao", lambda padrao: 1)
    aprovacoes_registradas = []
    monkeypatch.setattr(
        agente_nfs_e.historico_centros_funcionarios, "registrar_aprovacao_padrao_documento",
        lambda padrao, nota=None: aprovacoes_registradas.append(padrao),
    )
    if padrao_erp_categoria == "achou":
        monkeypatch.setattr(
            agente_nfs_e, "_tentar_inferir_padrao_do_erp",
            lambda access_token, contato_id, nome: {"categoria": {"id": "cat-erp", "nome": "Categoria ERP"}, "rateio": None, "evidencia_qtd": 3},
        )
    else:
        monkeypatch.setattr(agente_nfs_e, "_tentar_inferir_padrao_do_erp", lambda *a, **k: None)

    banco_padrao = {
        "conta_financeira_id": regras_nfse.CONTA_FINANCEIRA_ALFA_ID,
        "forma_pagamento": "transferência bancária",
        "data_vencimento": date(2026, 10, 8),
        "origem_banco": "camada1_registro_local",
        "permite_autonomia_banco": True,
    }
    monkeypatch.setattr(
        agente_nfs_e, "resolver_banco_e_vencimento",
        lambda *a, **k: info_banco if info_banco is not None else banco_padrao,
    )
    monkeypatch.setattr(
        agente_nfs_e.verificacao_duplicidade, "verificar_lancamento_similar_existe",
        lambda *a, **k: duplicata,
    )
    monkeypatch.setattr(
        agente_nfs_e, "criar_lancamento",
        lambda *a, **k: (status_lancamento, {"protocolo": "p1"}),
    )
    registrado = {}
    monkeypatch.setattr(
        agente_nfs_e.fornecedores_registry, "registrar_confirmado",
        lambda **kwargs: registrado.update(kwargs),
    )
    monkeypatch.setattr(agente_nfs_e.fornecedores_registry, "incrementar_uso_autonomo_banco", lambda cnpj: 1)
    # Histórico de retenção (2026-09-25): por padrão o fornecedor TEM
    # retenção no histórico - mantém o imposto como a IA leu, igual antes.
    monkeypatch.setattr(agente_nfs_e, "fornecedor_tem_historico_de_retencao", lambda access_token, contato_id: True)
    return registrado, aprovacoes_registradas


def test_camada3_fornecedor_totalmente_novo_nunca_lanca_automatico(monkeypatch):
    # Regressão do achado CRÍTICO na revisão pós-implementação: um
    # fornecedor nunca visto antes (sem registro local, sem padrão no
    # ERP) NÃO pode lançar automático mesmo com contato/categoria/banco
    # todos resolvendo com match exato.
    # NOTA (2026-09-08): o funil de categoria "de criar do zero" só é
    # alcançado quando o nome do arquivo não tem data reconhecível - com
    # data, a checagem da busca automática (nao_encontrado) sempre
    # bloqueia e retorna antes de chegar aqui (ver teste dedicado a isso).
    _mockar_dependencias_comuns(monkeypatch, registro_fornecedor=None, padrao_erp_categoria=None, tem_data_competencia=False)

    resultado = agente_nfs_e.processar_dados_nfse(_dados_base(), "NFSE_2026-10-01.pdf", "geral")

    assert resultado["sucesso"] is False
    assert "inédito" in resultado["motivo"]


def test_camada2_categoria_inferida_do_erp_pre_matura_mesmo_sem_lancar(monkeypatch):
    # Sem data de competência (único jeito de alcançar este funil hoje -
    # ver nota acima), o aprendizado (pré-maturação) da Camada 2 ainda
    # acontece mesmo que o lançamento em si não saia (falta a data).
    registrado, aprovacoes = _mockar_dependencias_comuns(monkeypatch, registro_fornecedor=None, padrao_erp_categoria="achou", tem_data_competencia=False)

    resultado = agente_nfs_e.processar_dados_nfse(_dados_base(), "NFSE_2026-10-01.pdf", "geral")

    assert resultado["sucesso"] is False
    assert "Data de competência" in resultado["motivo"]
    assert len(aprovacoes) == agente_nfs_e.historico_centros_funcionarios.MATURACAO_MINIMA_PARA_AUTONOMIA


def test_camada1_categoria_local_mas_padrao_nao_maduro_vai_para_revisao(monkeypatch):
    registro = {"categoria_id": "cat-1", "categoria_nome": "Categoria Aprendida"}
    _mockar_dependencias_comuns(monkeypatch, registro_fornecedor=registro, padrao_maduro=False, tem_data_competencia=False)

    resultado = agente_nfs_e.processar_dados_nfse(_dados_base(), "NFSE_2026-10-01.pdf", "geral")

    assert resultado["sucesso"] is False
    assert "não maturou" in resultado["motivo"]
    # CORREÇÃO (2026-09-22, bug real de produção - Saúde Exemplo): mesmo indo para
    # revisão manual (padrão ainda não maturou), o Funil de 3 Camadas já
    # resolveu contato/categoria certos via CNPJ (Camada 1) - isso precisa
    # chegar em `dados["contato"]`/`dados["categoria"]` para o painel
    # pré-selecionar o fornecedor certo, em vez de cair num fuzzy-match
    # cego do nome bruto extraído do PDF.
    assert resultado["detalhes"]["contato"] == {"id": "contato-1", "nome": _dados_base()["fornecedor_nome"]}
    assert resultado["detalhes"]["categoria"] == {"id": "cat-1", "nome": "Categoria Aprendida"}


def test_categoria_perfeita_lanca_automatico_mesmo_sem_achar_na_busca(monkeypatch):
    # ATUALIZADO (2026-09-18, pedido explícito do usuário: a busca
    # automática de NFS-e tomados NÃO está ativada para esta empresa na
    # Conta Azul, então nunca vai rodar e achar nada sozinha aqui). Com
    # categoria já aprendida E padrão maduro (Camada 1 completa) e nenhum
    # indício de duplicata, o agente agora lança automático como Despesa
    # via Financeiro mesmo sem nenhum lançamento parecido já existir na
    # Conta Azul (ver os testes de match parcial/exato abaixo para o caso
    # em que HÁ indício de duplicata - aí ainda vai para revisão manual).
    registro = {"categoria_id": "cat-1", "categoria_nome": "Categoria Aprendida"}
    _mockar_dependencias_comuns(monkeypatch, registro_fornecedor=registro, padrao_maduro=True)

    resultado = agente_nfs_e.processar_dados_nfse(_dados_base(), "NFSE_2026-10-01.pdf", "geral")

    assert resultado["sucesso"] is True
    assert "Lançado automaticamente" in resultado["motivo"]


def test_incrementa_uso_autonomo_banco_ao_completar_lancamento_ja_importado(monkeypatch):
    # O único caminho de lançamento automático que sobra é COMPLETAR (via
    # PATCH) um lançamento que a Conta Azul já importou sozinha - não mais
    # criar do zero (ver teste acima).
    item_importado = {"id": "parcela-1", "descricao": "NFS-e:123 - Consultoria", "total": 1000.0, "fornecedor": {"nome": "Fornecedor Teste Ltda"}}
    chamadas = []
    _mockar_dependencias_comuns(monkeypatch, status_importacao="exato", item_importado=item_importado)
    monkeypatch.setattr(
        agente_nfs_e.ca, "get",
        lambda access_token, caminho: {"versao": 3, "valor_composicao": {"valor_bruto": 1000.0}},
    )
    monkeypatch.setattr(agente_nfs_e.ca, "patch", lambda access_token, caminho, payload: {"versao": 4})
    monkeypatch.setattr(agente_nfs_e.fornecedores_registry, "incrementar_uso_autonomo_banco", lambda cnpj: chamadas.append(cnpj))

    resultado = agente_nfs_e.processar_dados_nfse(_dados_base(), "NFSE_2026-10-01.pdf", "geral")

    assert resultado["sucesso"] is True
    assert chamadas == ["12.345.678/0001-90"]


def test_vai_para_revisao_quando_categoria_nao_e_match_exato(monkeypatch):
    # Camada 1 sempre retorna "exato" internamente (categoria já aprendida
    # tem match exato por construção) - para testar um match não-exato de
    # verdade, força o fallback de Camada 3 (buscar_categoria) a devolver
    # "aproximado".
    _mockar_dependencias_comuns(monkeypatch, registro_fornecedor=None, padrao_erp_categoria=None, categoria_match="aproximado", tem_data_competencia=False)

    resultado = agente_nfs_e.processar_dados_nfse(_dados_base(), "NFSE_2026-10-01.pdf", "geral")

    assert resultado["sucesso"] is False
    assert "match exato" in resultado["motivo"]


def test_vai_para_revisao_quando_conferencia_de_valor_nao_bate(monkeypatch):
    registro = {"categoria_id": "cat-1", "categoria_nome": "Categoria Aprendida"}
    _mockar_dependencias_comuns(monkeypatch, registro_fornecedor=registro, padrao_maduro=True, tem_data_competencia=False)
    dados = _dados_base(valor_total=1000.0, valor_imposto=50.0, valor_liquido=900.0)  # 900+50 != 1000

    resultado = agente_nfs_e.processar_dados_nfse(dados, "NFSE_2026-10-01.pdf", "geral")

    assert resultado["sucesso"] is False
    assert "Conferência de valor" in resultado["motivo"]


def test_vai_para_revisao_quando_sem_data_competencia(monkeypatch):
    monkeypatch.setattr(agente_nfs_e, "extrair_data_do_nome_arquivo", lambda caminho: None)
    monkeypatch.setattr(agente_nfs_e, "obter_access_token", lambda: "tok")
    monkeypatch.setattr(
        agente_nfs_e, "buscar_contato_com_aprendizado",
        lambda access_token, nome, cnpj: ({"id": "contato-1", "nome": nome}, "exato"),
    )
    monkeypatch.setattr(agente_nfs_e.fornecedores_registry, "buscar_por_cnpj", lambda cnpj: None)
    monkeypatch.setattr(agente_nfs_e, "_tentar_inferir_padrao_do_erp", lambda *a, **k: None)
    monkeypatch.setattr(
        agente_nfs_e, "buscar_categoria",
        lambda access_token, nome: ({"id": "cat-1", "nome": nome}, "exato"),
    )

    resultado = agente_nfs_e.processar_dados_nfse(_dados_base(), "sem_data_no_nome.pdf", "geral")

    assert resultado["sucesso"] is False
    assert "competência" in resultado["motivo"]


def test_parece_nfse_direta_false_quando_prestacao_de_contas():
    dados = _dados_base(eh_prestacao_contas_de_funcionario=True)
    assert agente_nfs_e.parece_nfse_direta(dados) is False


def test_parece_nfse_direta_true_quando_nfse_normal():
    dados = _dados_base()
    assert agente_nfs_e.parece_nfse_direta(dados) is True


# --- rateio por centro de custo (2026-09-08, pedido do usuário) ------------------

def test_rateio_valido_e_aplicado_ao_lancar_automatico(monkeypatch):
    # ATUALIZADO (2026-09-18): rateio só é aplicado no caminho de CRIAR do
    # zero (a Conta Azul não deixa editar rateio de um lançamento já
    # existente via PATCH - ver completar_lancamento_existente). Antes,
    # criar do zero ficava bloqueado esperando a busca automática (ver
    # teste acima) - agora que esse bloqueio não existe mais, o rateio é
    # aplicado normalmente no lançamento criado.
    registro = {"categoria_id": "cat-1", "categoria_nome": "Categoria Aprendida"}
    _mockar_dependencias_comuns(monkeypatch, registro_fornecedor=registro, padrao_maduro=True)
    monkeypatch.setattr(
        agente_nfs_e, "validar_rateio_do_documento",
        lambda access_token, rateio_doc, valor_total: (
            [{"centro_custo_id": "cc-1", "centro_custo_nome": "1122 - Administrativo", "percentual": None, "valor": 1000.0}],
            [], [],
        ),
    )
    chamadas = []
    monkeypatch.setattr(
        agente_nfs_e, "criar_lancamento",
        lambda *a, rateio_centro_custo=None, **k: chamadas.append(rateio_centro_custo) or (200, {"protocolo": "p1"}),
    )
    dados = _dados_base(rateio_centro_custo_documento=[{"centro_custo": "1122 - Administrativo", "valor": 1000.0}])

    resultado = agente_nfs_e.processar_dados_nfse(dados, "NFSE_2026-10-01.pdf", "geral")

    assert resultado["sucesso"] is True
    assert chamadas[0][0]["centro_custo_id"] == "cc-1"


# --- integração com a busca automática de NFS-e tomados (2026-09-08) -------------

def test_numero_nfse_da_descricao_reconhece_variacoes():
    assert agente_nfs_e._numero_nfse_da_descricao("NFS-e:20248 - AB Soluções") == "20248"
    assert agente_nfs_e._numero_nfse_da_descricao("Compra de serviço Série:SN / NFS-e:00011") == "11"
    assert agente_nfs_e._numero_nfse_da_descricao("Aluguel de sala comercial") is None


def test_buscar_lancamento_ja_importado_exato(monkeypatch):
    item = {"id": "parcela-1", "descricao": "NFS-e:123 - Consultoria", "total": 1000.0, "fornecedor": {"nome": "Fornecedor Teste Ltda"}}
    monkeypatch.setattr(agente_nfs_e.ca, "get", lambda access_token, caminho, params=None: {"itens": [item]})

    status, encontrado = agente_nfs_e.buscar_lancamento_ja_importado("tok", "123", "Fornecedor Teste Ltda", 1000.0, date(2026, 10, 1))

    assert status == "exato"
    assert encontrado["id"] == "parcela-1"


def test_buscar_lancamento_ja_importado_parcial_quando_valor_diverge(monkeypatch):
    # Caso real trazido pelo usuário: fornecedor com valor fixo mensal
    # (ex: "Premium Turismo" R$5.000/mês) - numero+fornecedor batem mas o
    # valor da nota atual é diferente do lançamento achado -> revisão.
    item = {"id": "parcela-1", "descricao": "NFS-e:123 - Consultoria", "total": 1000.0, "fornecedor": {"nome": "Fornecedor Teste Ltda"}}
    monkeypatch.setattr(agente_nfs_e.ca, "get", lambda access_token, caminho, params=None: {"itens": [item]})

    status, encontrado = agente_nfs_e.buscar_lancamento_ja_importado("tok", "123", "Fornecedor Teste Ltda", 5000.0, date(2026, 10, 1))

    assert status == "parcial"


def test_buscar_lancamento_ja_importado_nao_encontrado_quando_fornecedor_diverge(monkeypatch):
    item = {"id": "parcela-1", "descricao": "NFS-e:123 - Consultoria", "total": 1000.0, "fornecedor": {"nome": "Outro Fornecedor Qualquer"}}
    monkeypatch.setattr(agente_nfs_e.ca, "get", lambda access_token, caminho, params=None: {"itens": [item]})

    status, encontrado = agente_nfs_e.buscar_lancamento_ja_importado("tok", "123", "Fornecedor Teste Ltda", 1000.0, date(2026, 10, 1))

    assert status == "nao_encontrado"


def test_buscar_lancamento_ja_importado_ambiguo_trata_como_nao_encontrado(monkeypatch):
    itens = [
        {"id": "parcela-1", "descricao": "NFS-e:123 - Consultoria", "total": 1000.0, "fornecedor": {"nome": "Fornecedor Teste Ltda"}},
        {"id": "parcela-2", "descricao": "NFS-e:123 - Consultoria (2a via)", "total": 1000.0, "fornecedor": {"nome": "Fornecedor Teste Ltda"}},
    ]
    monkeypatch.setattr(agente_nfs_e.ca, "get", lambda access_token, caminho, params=None: {"itens": itens})

    status, encontrado = agente_nfs_e.buscar_lancamento_ja_importado("tok", "123", "Fornecedor Teste Ltda", 1000.0, date(2026, 10, 1))

    assert status == "nao_encontrado"


def test_completa_lancamento_ja_importado_em_vez_de_criar(monkeypatch):
    item_importado = {"id": "parcela-1", "descricao": "NFS-e:123 - Consultoria", "total": 1000.0, "fornecedor": {"nome": "Fornecedor Teste Ltda"}}
    registro = {"categoria_id": "cat-1", "categoria_nome": "Categoria Aprendida"}
    _mockar_dependencias_comuns(
        monkeypatch, registro_fornecedor=registro, padrao_maduro=True,
        status_importacao="exato", item_importado=item_importado,
    )
    monkeypatch.setattr(
        agente_nfs_e.ca, "get",
        lambda access_token, caminho: {"versao": 3, "valor_composicao": {"valor_bruto": 1000.0}},
    )
    chamadas_criar = []
    monkeypatch.setattr(agente_nfs_e, "criar_lancamento", lambda *a, **k: chamadas_criar.append(1) or (200, {"protocolo": "nao_deveria_chamar"}))
    chamadas_patch = []
    monkeypatch.setattr(agente_nfs_e.ca, "patch", lambda access_token, caminho, payload: chamadas_patch.append(payload) or {"versao": 4})

    resultado = agente_nfs_e.processar_dados_nfse(_dados_base(), "NFSE_2026-10-01.pdf", "geral")

    assert resultado["sucesso"] is True
    assert resultado["detalhes"]["id_parcela"] == "parcela-1"
    assert not chamadas_criar
    assert len(chamadas_patch) == 1
    assert chamadas_patch[0]["versao"] == 3


def test_match_parcial_com_lancamento_existente_vai_para_revisao(monkeypatch):
    # ATUALIZADO (2026-09-18): match parcial não bloqueia mais a criação -
    # só entra como aviso de possível duplicata (motivos_pendentes), o que
    # ainda impede o lançamento 100% automático e força revisão manual na
    # tela normal de NFS-e (tipo_documento="nfse", COM botão de aprovar).
    item_importado = {"id": "parcela-1", "descricao": "NFS-e:123 - Consultoria", "total": 5000.0, "fornecedor": {"nome": "Fornecedor Teste Ltda"}}
    _mockar_dependencias_comuns(monkeypatch, status_importacao="parcial", item_importado=item_importado)

    resultado = agente_nfs_e.processar_dados_nfse(_dados_base(), "NFSE_2026-10-01.pdf", "geral")

    assert resultado["sucesso"] is False
    assert "já existe" in resultado["motivo"]
    assert "DUPLICATA" in resultado["motivo"]
    assert resultado["detalhes"]["tipo_documento"] == "nfse"


def test_exato_sem_banco_conhecido_vai_para_revisao_com_aviso_de_duplicata(monkeypatch):
    # ATUALIZADO (2026-09-18): match exato sem banco resolvido não bloqueia
    # mais a criação - só entra como aviso de possível duplicata, forçando
    # revisão manual na tela normal de NFS-e (COM botão de aprovar) em vez
    # da antiga tela somente informativa.
    item_importado = {"id": "parcela-1", "descricao": "NFS-e:123 - Consultoria", "total": 1000.0, "fornecedor": {"nome": "Fornecedor Teste Ltda"}}
    info_banco_camada3 = {
        "conta_financeira_id": None, "forma_pagamento": "transferência bancária",
        "data_vencimento": date(2026, 10, 8), "origem_banco": "camada3_fallback",
        "permite_autonomia_banco": False,
    }
    _mockar_dependencias_comuns(
        monkeypatch, status_importacao="exato", item_importado=item_importado, info_banco=info_banco_camada3,
    )

    resultado = agente_nfs_e.processar_dados_nfse(_dados_base(), "NFSE_2026-10-01.pdf", "geral")

    assert resultado["sucesso"] is False
    assert "DUPLICATA" in resultado["motivo"]
    assert resultado["detalhes"]["tipo_documento"] == "nfse"
    assert resultado["detalhes"]["id_lancamento_existente"] == "parcela-1"


def test_vai_para_revisao_quando_rateio_tem_erro(monkeypatch):
    registro = {"categoria_id": "cat-1", "categoria_nome": "Categoria Aprendida"}
    _mockar_dependencias_comuns(monkeypatch, registro_fornecedor=registro, padrao_maduro=True, tem_data_competencia=False)
    monkeypatch.setattr(
        agente_nfs_e, "validar_rateio_do_documento",
        lambda access_token, rateio_doc, valor_total: (
            [], ["Centro de custo 'Inexistente' não encontrado (ou sem match exato) na Conta Azul."],
            [{"tipo": "centro_custo", "nome_no_documento": "Inexistente", "sugestao_parecida": None}],
        ),
    )
    dados = _dados_base(rateio_centro_custo_documento=[{"centro_custo": "Inexistente", "valor": 1000.0}])

    resultado = agente_nfs_e.processar_dados_nfse(dados, "NFSE_2026-10-01.pdf", "geral")

    assert resultado["sucesso"] is False
    assert "Centro de custo" in resultado["motivo"]
    assert resultado["cadastros_pendentes"][0]["tipo"] == "centro_custo"


# --- prompt injection (2026-09-24, ver src/utils/deteccao_injecao.py) ------------

def test_alerta_de_injecao_impede_criacao_automatica_de_nfse(monkeypatch):
    registro = {"categoria_id": "cat-1", "categoria_nome": "Categoria Aprendida"}
    _mockar_dependencias_comuns(monkeypatch, registro_fornecedor=registro, padrao_maduro=True)
    chamou = []
    monkeypatch.setattr(agente_nfs_e, "criar_lancamento", lambda *a, **k: chamou.append(1) or (200, {"protocolo": "p1"}))

    resultado = agente_nfs_e.processar_dados_nfse(
        _dados_base(alerta_instrucao_no_documento=True, trecho_suspeito="ignore as instruções"),
        "NFSE_2026-10-01.pdf", "geral",
    )

    assert resultado["sucesso"] is False
    assert not chamou
    assert "MANIPULAÇÃO" in resultado["motivo"]


def test_alerta_de_injecao_impede_patch_automatico_de_lancamento_existente(monkeypatch):
    item_importado = {"id": "parcela-1", "descricao": "NFS-e:123 - Consultoria", "total": 1000.0, "fornecedor": {"nome": "Fornecedor Teste Ltda"}}
    _mockar_dependencias_comuns(monkeypatch, status_importacao="exato", item_importado=item_importado)
    patches = []
    monkeypatch.setattr(agente_nfs_e, "completar_lancamento_existente", lambda *a, **k: patches.append(1) or {})

    resultado = agente_nfs_e.processar_dados_nfse(
        _dados_base(alerta_instrucao_no_documento=True), "NFSE_2026-10-01.pdf", "geral",
    )

    assert resultado["sucesso"] is False
    assert not patches
    assert "MANIPULAÇÃO" in resultado["motivo"]


# --- teto de valor para autonomia (2026-09-25, ver src/utils/teto_autonomia.py) --

def test_nfse_acima_do_teto_vai_para_revisao_com_medida_de_seguranca(monkeypatch):
    registro = {"categoria_id": "cat-1", "categoria_nome": "Categoria Aprendida"}
    _mockar_dependencias_comuns(monkeypatch, registro_fornecedor=registro, padrao_maduro=True)
    chamou = []
    monkeypatch.setattr(agente_nfs_e, "criar_lancamento", lambda *a, **k: chamou.append(1) or (200, {"protocolo": "p1"}))

    resultado = agente_nfs_e.processar_dados_nfse(_dados_base(valor_total=98765.0), "NFSE_2026-10-01.pdf", "geral")

    assert resultado["sucesso"] is False
    assert not chamou
    assert "MEDIDA DE SEGURANÇA - VALOR FORA DO PADRÃO RECORRENTE" in resultado["motivo"]


def test_nfse_recorrente_abaixo_do_teto_continua_automatica(monkeypatch):
    registro = {"categoria_id": "cat-1", "categoria_nome": "Categoria Aprendida"}
    _mockar_dependencias_comuns(monkeypatch, registro_fornecedor=registro, padrao_maduro=True)

    resultado = agente_nfs_e.processar_dados_nfse(_dados_base(valor_total=41173.80), "NFSE_2026-10-01.pdf", "geral")

    assert resultado["sucesso"] is True


def test_nfse_acima_do_teto_bloqueia_patch_automatico_de_lancamento_existente(monkeypatch):
    item_importado = {"id": "parcela-1", "descricao": "NFS-e:123 - Consultoria", "total": 98765.0, "fornecedor": {"nome": "Fornecedor Teste Ltda"}}
    _mockar_dependencias_comuns(monkeypatch, status_importacao="exato", item_importado=item_importado)
    patches = []
    monkeypatch.setattr(agente_nfs_e, "completar_lancamento_existente", lambda *a, **k: patches.append(1) or {})

    resultado = agente_nfs_e.processar_dados_nfse(_dados_base(valor_total=98765.0), "NFSE_2026-10-01.pdf", "geral")

    assert resultado["sucesso"] is False
    assert not patches
    assert "MEDIDA DE SEGURANÇA" in resultado["motivo"]


def test_nfse_saude_acima_do_teto_lanca_normalmente_por_ser_isenta(monkeypatch):
    registro = {"categoria_id": "cat-1", "categoria_nome": "4.03.005. Plano de Saúde"}
    _mockar_dependencias_comuns(monkeypatch, registro_fornecedor=registro, padrao_maduro=True)
    dados = _dados_base(valor_total=162992.24, fornecedor_nome="Saúde Exemplo", fornecedor_cnpj="44.555.666/0001-77")

    resultado = agente_nfs_e.processar_dados_nfse(dados, "NFSE_2026-10-01.pdf", "geral")

    assert resultado["sucesso"] is True
    assert "MEDIDA DE SEGURANÇA" not in resultado["motivo"]


def test_saude_isenta_do_teto_continua_barrada_por_suspeita_de_injecao(monkeypatch):
    registro = {"categoria_id": "cat-1", "categoria_nome": "4.03.005. Plano de Saúde"}
    _mockar_dependencias_comuns(monkeypatch, registro_fornecedor=registro, padrao_maduro=True)
    dados = _dados_base(valor_total=162992.24, fornecedor_cnpj="44.555.666/0001-77", alerta_instrucao_no_documento=True)

    resultado = agente_nfs_e.processar_dados_nfse(dados, "NFSE_2026-10-01.pdf", "geral")

    assert resultado["sucesso"] is False
    assert "MANIPULAÇÃO" in resultado["motivo"]


# --- retenção seguindo o histórico do fornecedor (2026-09-25) --------------------

def _capturar_criar_lancamento(monkeypatch):
    chamadas = []
    monkeypatch.setattr(
        agente_nfs_e, "criar_lancamento",
        lambda *a, valor_liquido=None, **k: chamadas.append(valor_liquido) or (200, {"protocolo": "p1"}),
    )
    return chamadas


def test_saude_sem_retencao_no_historico_lanca_pelo_valor_cheio(monkeypatch):
    # Caso real: a IA leu o "Valor ISS" devido pela própria Saúde Exemplo (R$ 3.259,84,
    # mesmo valor da multa do boleto) como retido. No histórico a Saúde Exemplo nunca
    # teve retenção - lança pelo valor cheio, como sempre foi lançada.
    registro = {"categoria_id": "cat-1", "categoria_nome": "4.03.005. Plano de Saúde"}
    _mockar_dependencias_comuns(monkeypatch, registro_fornecedor=registro, padrao_maduro=True)
    monkeypatch.setattr(agente_nfs_e, "fornecedor_tem_historico_de_retencao", lambda access_token, contato_id: False)
    chamadas = _capturar_criar_lancamento(monkeypatch)
    dados = _dados_base(valor_total=162992.24, valor_imposto=3259.84, valor_liquido=159732.40, fornecedor_cnpj="44.555.666/0001-77")

    resultado = agente_nfs_e.processar_dados_nfse(dados, "NFSE_2026-10-01.pdf", "geral")

    assert resultado["sucesso"] is True
    assert chamadas == [None]  # sem líquido -> lançamento pelo valor cheio
    assert dados["imposto_ignorado_pelo_historico"] == 3259.84


def test_fornecedor_com_retencao_no_historico_mantem_o_liquido(monkeypatch):
    registro = {"categoria_id": "cat-1", "categoria_nome": "Honorários"}
    _mockar_dependencias_comuns(monkeypatch, registro_fornecedor=registro, padrao_maduro=True)
    chamadas = _capturar_criar_lancamento(monkeypatch)
    dados = _dados_base(valor_total=23000.0, valor_imposto=545.10, valor_liquido=22454.90)

    agente_nfs_e.processar_dados_nfse(dados, "NFSE_2026-10-01.pdf", "geral")

    assert chamadas == [22454.90]


def test_consulta_de_historico_falhou_mantem_o_que_a_ia_leu(monkeypatch):
    registro = {"categoria_id": "cat-1", "categoria_nome": "Honorários"}
    _mockar_dependencias_comuns(monkeypatch, registro_fornecedor=registro, padrao_maduro=True)
    monkeypatch.setattr(agente_nfs_e, "fornecedor_tem_historico_de_retencao", lambda access_token, contato_id: None)
    chamadas = _capturar_criar_lancamento(monkeypatch)

    agente_nfs_e.processar_dados_nfse(_dados_base(valor_total=1000.0, valor_imposto=50.0, valor_liquido=950.0), "NFSE_2026-10-01.pdf", "geral")

    assert chamadas == [950.0]


def test_sem_imposto_ou_sem_fornecedor_confirmado_nem_consulta_historico(monkeypatch):
    consultas = []
    monkeypatch.setattr(agente_nfs_e, "fornecedor_tem_historico_de_retencao", lambda *a: consultas.append(1) or False)
    dados = _dados_base(valor_imposto=None)
    agente_nfs_e.aplicar_padrao_historico_de_retencao("tok", dados, {"id": "c1", "nome": "X"}, "exato")
    dados_aprox = _dados_base(valor_imposto=50.0, valor_liquido=950.0)
    agente_nfs_e.aplicar_padrao_historico_de_retencao("tok", dados_aprox, {"id": "c1", "nome": "X"}, "aproximado")
    assert not consultas
    assert dados_aprox["valor_liquido"] == 950.0


def test_historico_de_retencao_reconhece_lancamentos_de_imposto_do_fornecedor(monkeypatch):
    itens = [
        {"fornecedor": {"id": "arte"}, "descricao": "NFS-e:3 - Serviços de consultoria"},
        {"fornecedor": {"id": "arte"}, "descricao": "ISS - NFS-e:3 - Arte Engenharia"},
        {"fornecedor": {"id": "saude"}, "descricao": "NFS-e 10000001 - Plano de saúde"},
        {"fornecedor": {"id": "outro"}, "descricao": "PIS/COFINS/CSLL - NFS-e:11"},
    ]
    monkeypatch.setattr(agente_nfs_e.ca, "get", lambda *a, **k: {"itens": itens})
    assert agente_nfs_e.fornecedor_tem_historico_de_retencao("tok", "arte") is True
    assert agente_nfs_e.fornecedor_tem_historico_de_retencao("tok", "saude") is False

    def _falha(*a, **k):
        raise RuntimeError("rede")
    monkeypatch.setattr(agente_nfs_e.ca, "get", _falha)
    assert agente_nfs_e.fornecedor_tem_historico_de_retencao("tok", "arte") is None
