from src.agentes import maestro
from src.agentes.agente_nfs_e import NFSeExtracaoIncompleta


# --- parece_resumo_de_folha ------------------------------------------------------

def test_parece_resumo_de_folha_com_tipo_e_rateio():
    assert maestro.parece_resumo_de_folha({"tabelas": [{"tipo": "x", "rateio": [{"centro_custo": "1", "valor": 1.0}]}]})


def test_parece_resumo_de_folha_com_tipo_em_regras_folha_mesmo_sem_rateio():
    assert maestro.parece_resumo_de_folha({"tabelas": [{"tipo": "fgts", "rateio": []}]})


def test_parece_resumo_de_folha_falso_quando_tipo_e_nulo_mesmo_com_rateio():
    # Caso real de produção (2026-09-14): boleto de fornecedor (Link Exemplo
    # Tecnologia, fatura de internet) com uma anotação bancária de "Centro
    # Custo/Resultado" no recibo - o prompt de extração de folha vasculha
    # esse tipo de campo em QUALQUER documento e força uma linha de
    # "rateio", mesmo quando ele mesmo concluiu "tipo": null (documento sem
    # nenhuma relação com folha de pagamento, por definição do prompt).
    # Confiar só em "rateio" preenchido classificava esse boleto como
    # folha, e o documento nunca chegava em agente_integrado.processar_
    # documento - onde o fornecedor É buscado por CNPJ/nome.
    dados_folha = {
        "tabelas": [{
            "tipo": None,
            "rateio": [{"centro_custo": "1126", "valor": 275.0}],
            "contexto_bruto": "BANCO ALFA | RECIBO DO PAGADOR | Centro Custo/Resultado: 1126",
        }],
    }
    assert maestro.parece_resumo_de_folha(dados_folha) is False


def test_parece_resumo_de_folha_falso_sem_tabelas():
    assert maestro.parece_resumo_de_folha({"tabelas": []}) is False


def _mockar_folha(monkeypatch, dados_folha, resultados_por_tabela):
    monkeypatch.setattr(maestro, "extrair_dados_folha", lambda caminho, max_tentativas=5: dados_folha)
    monkeypatch.setattr(
        maestro, "processar_documento_folha",
        lambda dados_folha, usuario, caminho: resultados_por_tabela,
    )


def _mockar_sem_folha(monkeypatch):
    monkeypatch.setattr(maestro, "extrair_dados_folha", lambda caminho, max_tentativas=5: None)


class _AgenteNfseFalso:
    """Substitui o módulo agente_nfs_e inteiro nos testes que não devem
    chegar nele (roteamento pra comum) ou que querem controlar a extração
    sem chamar o Gemini de verdade."""

    # Precisa ser a exceção REAL (não uma classe fake qualquer) - o
    # maestro escreve `except agente_nfs_e.NFSeExtracaoIncompleta as e:`,
    # e Python resolve esse nome no momento em que a exceção é levantada.
    NFSeExtracaoIncompleta = NFSeExtracaoIncompleta

    def __init__(self, dados_nfse=None, deve_falhar_extracao=False, erro_extracao_incompleta=None):
        self.dados_nfse = dados_nfse
        self.deve_falhar_extracao = deve_falhar_extracao
        self.erro_extracao_incompleta = erro_extracao_incompleta
        self.chamado_com = None

    def extrair_dados_nfse(self, caminho, max_tentativas=5):
        if self.erro_extracao_incompleta is not None:
            raise self.erro_extracao_incompleta
        if self.deve_falhar_extracao:
            raise RuntimeError("não parece NFS-e")
        return self.dados_nfse

    def parece_nfse_direta(self, dados):
        return not dados.get("eh_prestacao_contas_de_funcionario", False)

    def processar_dados_nfse(self, dados, caminho, usuario):
        self.chamado_com = (dados, caminho, usuario)
        return {"sucesso": True, "motivo": "lançado", "detalhes": dados}


def test_roteia_para_folha_quando_parece_resumo_de_folha(monkeypatch):
    dados_folha = {"tabelas": [{"tipo": "fgts", "rateio": []}]}
    resultados = {"fgts": {"sucesso": True, "motivo": "lançado", "detalhes": {}}}
    _mockar_folha(monkeypatch, dados_folha, resultados)

    resultado = maestro.classificar_e_processar_documento("qualquer.pdf")

    assert resultado["sucesso"] is True


def test_roteia_para_nfse_quando_extracao_de_nfse_bate(monkeypatch):
    _mockar_sem_folha(monkeypatch)
    agente_falso = _AgenteNfseFalso(dados_nfse={"numero_nota": "1", "eh_prestacao_contas_de_funcionario": False})
    monkeypatch.setattr(maestro, "agente_nfs_e", agente_falso)

    resultado = maestro.classificar_e_processar_documento("nota.pdf", usuario="geral")

    assert resultado["sucesso"] is True
    assert agente_falso.chamado_com is not None


def test_nfse_dentro_de_prestacao_contas_cai_no_fluxo_comum(monkeypatch):
    # Caso de borda real encontrado na varredura do histórico de produção:
    # uma NFS-e pode estar citada DENTRO de uma prestação de contas de
    # colaborador - isso precisa continuar no fluxo de reembolso
    # (autonomia zero), nunca no agente_nfs_e.
    _mockar_sem_folha(monkeypatch)
    agente_falso = _AgenteNfseFalso(dados_nfse={"numero_nota": "1", "eh_prestacao_contas_de_funcionario": True})
    monkeypatch.setattr(maestro, "agente_nfs_e", agente_falso)
    monkeypatch.setattr(maestro, "processar_documento", lambda caminho, usuario: {"sucesso": False, "motivo": "reembolso", "detalhes": {}})

    resultado = maestro.classificar_e_processar_documento("reembolso.pdf")

    assert agente_falso.chamado_com is None
    assert resultado["motivo"] == "reembolso"


def test_roteia_para_documento_comum_quando_nao_parece_folha_nem_nfse(monkeypatch):
    _mockar_sem_folha(monkeypatch)
    agente_falso = _AgenteNfseFalso(deve_falhar_extracao=True)
    monkeypatch.setattr(maestro, "agente_nfs_e", agente_falso)
    monkeypatch.setattr(maestro, "processar_documento", lambda caminho, usuario: {"sucesso": True, "motivo": "comum", "detalhes": {}})

    resultado = maestro.classificar_e_processar_documento("fatura.pdf")

    assert resultado["motivo"] == "comum"


_TABELA_FOLHA_GENERICA = {"tipo": "x", "rateio": [{"centro_custo": "1122", "valor": 1.0}]}


def test_sucesso_geral_false_quando_todas_tabelas_ignoradas_propositalmente(monkeypatch):
    # Regressão (2026-09-03, falso positivo real de produção): um documento
    # cuja ÚNICA tabela veio "ignorada de propósito" (ex: Pró-labore não
    # circulada) NÃO pode ser marcado como sucesso.
    resultados = {
        "pro_labore": {
            "sucesso": False, "motivo": "não circulada - ignorada propositalmente",
            "ignorado_propositalmente": True, "detalhes": {},
        },
    }
    _mockar_folha(monkeypatch, {"tabelas": [_TABELA_FOLHA_GENERICA]}, resultados)

    resultado = maestro.classificar_e_processar_documento("qualquer.pdf")

    assert resultado["sucesso"] is False


def test_sucesso_geral_true_quando_ha_tabela_relevante_lancada_com_sucesso(monkeypatch):
    resultados = {
        "inss": {"sucesso": True, "motivo": "lançado", "detalhes": {"protocolo": "123"}},
        "pro_labore": {
            "sucesso": False, "motivo": "não circulada - ignorada propositalmente",
            "ignorado_propositalmente": True, "detalhes": {},
        },
    }
    _mockar_folha(monkeypatch, {"tabelas": [_TABELA_FOLHA_GENERICA]}, resultados)

    resultado = maestro.classificar_e_processar_documento("qualquer.pdf")

    assert resultado["sucesso"] is True


def test_sucesso_geral_false_quando_tabela_relevante_falhou(monkeypatch):
    resultados = {
        "termo_rescisao": {"sucesso": False, "motivo": "categoria não encontrada", "detalhes": {}},
        "pro_labore": {
            "sucesso": False, "motivo": "não circulada - ignorada propositalmente",
            "ignorado_propositalmente": True, "detalhes": {},
        },
    }
    _mockar_folha(monkeypatch, {"tabelas": [_TABELA_FOLHA_GENERICA]}, resultados)

    resultado = maestro.classificar_e_processar_documento("qualquer.pdf")

    assert resultado["sucesso"] is False


def test_nfse_com_extracao_incompleta_vai_direto_para_revisao_sem_cair_em_comum(monkeypatch):
    # Regressão (2026-09-08, achado em revisão pós-implementação): antes,
    # QUALQUER falha na extração de NFS-e (mesmo uma que reconheceu a nota
    # mas faltou completar um campo secundário) caía silenciosamente no
    # fluxo de documento comum - podendo classificar tudo errado sem
    # avisar ninguém. Agora precisa ir direto pra revisão.
    _mockar_sem_folha(monkeypatch)
    erro = NFSeExtracaoIncompleta("faltou boleto_data_vencimento", {"numero_nota": "123", "fornecedor_nome": "X"})
    agente_falso = _AgenteNfseFalso(erro_extracao_incompleta=erro)
    monkeypatch.setattr(maestro, "agente_nfs_e", agente_falso)
    chamado_comum = {"foi": False}
    monkeypatch.setattr(maestro, "processar_documento", lambda caminho, usuario: chamado_comum.update(foi=True) or {"sucesso": True, "motivo": "comum", "detalhes": {}})

    resultado = maestro.classificar_e_processar_documento("nota_incompleta.pdf")

    assert resultado["sucesso"] is False
    assert "incompleta" in resultado["motivo"].lower()
    assert resultado["detalhes"]["tipo_documento"] == "nfse"
    assert resultado["detalhes"]["numero_nota"] == "123"
    assert chamado_comum["foi"] is False


# --- confirmação de NFS-e antes de rotear (2026-09-25, ver classificacao_documento.py) --

def _mockar_comum(monkeypatch):
    chamado = {"foi": False}
    monkeypatch.setattr(
        maestro, "processar_documento",
        lambda caminho, usuario: chamado.update(foi=True) or {"sucesso": True, "motivo": "comum", "detalhes": {}},
    )
    return chamado


def test_fatura_aceita_pelo_extrator_mas_declarada_conta_de_consumo_vai_para_comum(monkeypatch):
    _mockar_sem_folha(monkeypatch)
    agente_falso = _AgenteNfseFalso(dados_nfse={"numero_nota": "ENERGIA EXEMPLO-1", "codigo_servico": "04.22", "tipo_documento_detectado": "conta_consumo"})
    monkeypatch.setattr(maestro, "agente_nfs_e", agente_falso)
    chamado = _mockar_comum(monkeypatch)

    resultado = maestro.classificar_e_processar_documento("energia_exemplo.pdf")

    assert agente_falso.chamado_com is None
    assert chamado["foi"] and resultado["motivo"] == "comum"


def test_fatura_sem_marca_de_nfse_no_texto_vai_para_comum_mesmo_com_ia_dizendo_nfse(monkeypatch):
    _mockar_sem_folha(monkeypatch)
    agente_falso = _AgenteNfseFalso(dados_nfse={"numero_nota": "58802519", "codigo_servico": "N/D", "tipo_documento_detectado": "nfse"})
    monkeypatch.setattr(maestro, "agente_nfs_e", agente_falso)
    monkeypatch.setattr(maestro.classificacao_documento, "extrair_texto_pdf", lambda caminho: "FATURA Plano empresarial " * 10)
    chamado = _mockar_comum(monkeypatch)

    maestro.classificar_e_processar_documento("fatura.pdf")

    assert agente_falso.chamado_com is None
    assert chamado["foi"]


def test_nfse_com_marca_no_texto_continua_indo_para_o_agente_de_nfse(monkeypatch):
    _mockar_sem_folha(monkeypatch)
    agente_falso = _AgenteNfseFalso(dados_nfse={"numero_nota": "10000001", "tipo_documento_detectado": "nfse"})
    monkeypatch.setattr(maestro, "agente_nfs_e", agente_falso)
    monkeypatch.setattr(maestro.classificacao_documento, "extrair_texto_pdf", lambda caminho: "NOTA FISCAL DE SERVIÇOS ELETRÔNICA - NFS-e " * 5)

    resultado = maestro.classificar_e_processar_documento("saude.pdf")

    assert agente_falso.chamado_com is not None
    assert resultado["sucesso"] is True


def test_nfse_incompleta_sem_marca_no_texto_vai_para_comum_em_vez_de_revisao_como_nfse(monkeypatch):
    _mockar_sem_folha(monkeypatch)
    erro = NFSeExtracaoIncompleta("faltou boleto_data_vencimento", {"numero_nota": "100200", "codigo_servico": "825922"})
    monkeypatch.setattr(maestro, "agente_nfs_e", _AgenteNfseFalso(erro_extracao_incompleta=erro))
    monkeypatch.setattr(maestro.classificacao_documento, "extrair_texto_pdf", lambda caminho: "Boleto Link Exemplo Linha digitavel 34191 Beneficiario " * 5)
    chamado = _mockar_comum(monkeypatch)

    resultado = maestro.classificar_e_processar_documento("boleto.pdf")

    assert chamado["foi"] and resultado["motivo"] == "comum"
