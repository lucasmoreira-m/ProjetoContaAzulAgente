from src.agentes import monitor_pasta


def test_bloqueia_reprocessamento_quando_arquivo_identico_ja_teve_sucesso(monkeypatch):
    registro_anterior = {
        "sucesso": True, "processado_em": "01/01/2026 10:00:00", "protocolo": "123",
    }
    monkeypatch.setattr(
        monitor_pasta.verificacao_duplicidade, "verificar_arquivo_ja_processado",
        lambda caminho: registro_anterior,
    )

    resultado = monitor_pasta.processar_documento_com_classificacao("qualquer.pdf")

    assert resultado["sucesso"] is False
    assert "DUPLICATA" in resultado["motivo"]
    assert resultado["_pular_registro_hash"] is True


def test_delega_ao_maestro_quando_arquivo_e_novidade(monkeypatch):
    monkeypatch.setattr(
        monitor_pasta.verificacao_duplicidade, "verificar_arquivo_ja_processado",
        lambda caminho: None,
    )
    chamado_com = {}

    def _maestro_falso(caminho, usuario):
        chamado_com["caminho"] = caminho
        chamado_com["usuario"] = usuario
        return {"sucesso": True, "motivo": "lançado", "detalhes": {}}

    monkeypatch.setattr(monitor_pasta.maestro, "classificar_e_processar_documento", _maestro_falso)

    resultado = monitor_pasta.processar_documento_com_classificacao("qualquer.pdf", "geral")

    assert resultado["sucesso"] is True
    assert chamado_com == {"caminho": "qualquer.pdf", "usuario": "geral"}


def test_nao_bloqueia_reprocessamento_quando_resultado_anterior_foi_falha(monkeypatch):
    # Falha/sucesso PARCIAL anterior deixa reprocessar (ver docstring de
    # processar_documento_com_classificacao) - só SUCESSO TOTAL bloqueia.
    registro_anterior = {"sucesso": False, "processado_em": "01/01/2026 10:00:00", "motivo": "categoria não encontrada"}
    monkeypatch.setattr(
        monitor_pasta.verificacao_duplicidade, "verificar_arquivo_ja_processado",
        lambda caminho: registro_anterior,
    )
    monkeypatch.setattr(
        monitor_pasta.maestro, "classificar_e_processar_documento",
        lambda caminho, usuario: {"sucesso": True, "motivo": "lançado", "detalhes": {}},
    )

    resultado = monitor_pasta.processar_documento_com_classificacao("qualquer.pdf")

    assert resultado["sucesso"] is True
