import threading

from src.utils import historico_centros_funcionarios as hcf


def test_registrar_e_buscar_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setattr(hcf, "CAMINHO_REGISTRO", tmp_path / "historico_centros_funcionarios.json")

    assert hcf.buscar_centro_custo_funcionario("Maria Exemplo Souza") is None

    hcf.registrar_centro_custo_funcionario("Maria Exemplo Souza", "id-1122", "1122 - Administrativo")

    registro = hcf.buscar_centro_custo_funcionario("MARIA EXEMPLO SOUZA")
    assert registro is not None
    assert registro["centro_custo_id"] == "id-1122"
    assert registro["centro_custo_nome"] == "1122 - Administrativo"
    assert registro["vezes_usado"] == 1


def test_registrar_de_novo_incrementa_vezes_usado(tmp_path, monkeypatch):
    monkeypatch.setattr(hcf, "CAMINHO_REGISTRO", tmp_path / "historico_centros_funcionarios.json")

    hcf.registrar_centro_custo_funcionario("Fulano de Tal", "id-1", "1121 - Diretoria")
    hcf.registrar_centro_custo_funcionario("Fulano de Tal", "id-1", "1121 - Diretoria")

    registro = hcf.buscar_centro_custo_funcionario("Fulano de Tal")
    assert registro["vezes_usado"] == 2


def test_nome_invalido_nao_registra(tmp_path, monkeypatch):
    monkeypatch.setattr(hcf, "CAMINHO_REGISTRO", tmp_path / "historico_centros_funcionarios.json")

    hcf.registrar_centro_custo_funcionario("", "id-1", "1121 - Diretoria")
    assert not (tmp_path / "historico_centros_funcionarios.json").exists()


def test_padrao_documento_novo_tem_contagem_zero(tmp_path, monkeypatch):
    monkeypatch.setattr(hcf, "CAMINHO_REGISTRO", tmp_path / "historico_centros_funcionarios.json")

    assert hcf.buscar_padrao_documento("rescisao") is None
    assert hcf.contagem_aprovacoes_padrao("rescisao") == 0


def test_registrar_aprovacao_padrao_incrementa_contagem(tmp_path, monkeypatch):
    monkeypatch.setattr(hcf, "CAMINHO_REGISTRO", tmp_path / "historico_centros_funcionarios.json")

    hcf.registrar_aprovacao_padrao_documento("rescisao")
    hcf.registrar_aprovacao_padrao_documento("RESCISÃO")  # mesmo padrão, normalizado
    entrada = hcf.registrar_aprovacao_padrao_documento("rescisao")

    assert entrada["contagem_aprovacoes"] == 3
    assert hcf.contagem_aprovacoes_padrao("rescisao") == 3


def test_registrar_aprovacao_padrao_acumula_notas_diretriz(tmp_path, monkeypatch):
    monkeypatch.setattr(hcf, "CAMINHO_REGISTRO", tmp_path / "historico_centros_funcionarios.json")

    hcf.registrar_aprovacao_padrao_documento("rescisao", "Conferir sempre a data do TRCT")
    entrada = hcf.registrar_aprovacao_padrao_documento("rescisao", "Confirmar centro de custo 3273")

    textos = [n["texto"] for n in entrada["notas_diretriz_humana"]]
    assert textos == ["Conferir sempre a data do TRCT", "Confirmar centro de custo 3273"]


def test_registrar_aprovacao_padrao_sem_nota_nao_adiciona_nota_vazia(tmp_path, monkeypatch):
    monkeypatch.setattr(hcf, "CAMINHO_REGISTRO", tmp_path / "historico_centros_funcionarios.json")

    entrada = hcf.registrar_aprovacao_padrao_documento("rescisao")
    assert entrada["notas_diretriz_humana"] == []

    entrada = hcf.registrar_aprovacao_padrao_documento("rescisao", "   ")
    assert entrada["notas_diretriz_humana"] == []


def test_padroes_documento_nao_colide_com_nomes_de_funcionario(tmp_path, monkeypatch):
    monkeypatch.setattr(hcf, "CAMINHO_REGISTRO", tmp_path / "historico_centros_funcionarios.json")

    hcf.registrar_centro_custo_funcionario("Fulano de Tal", "id-1", "1121 - Diretoria")
    hcf.registrar_aprovacao_padrao_documento("rescisao")

    assert hcf.buscar_centro_custo_funcionario("Fulano de Tal")["centro_custo_id"] == "id-1"
    assert hcf.contagem_aprovacoes_padrao("rescisao") == 1


def test_registrar_aprovacao_padrao_concorrente_nao_perde_atualizacoes(tmp_path, monkeypatch):
    # Regressão (2026-09-03, risco real apontado em auditoria): antes do
    # FileLock, duas threads fazendo leitura-modificação-escrita do MESMO
    # arquivo ao mesmo tempo podiam se sobrescrever (last write wins),
    # perdendo incrementos da contagem de maturação - exatamente o cenário
    # real do ecossistema unificado (thread do monitor + thread do painel
    # Streamlit no MESMO processo). Dispara N threads reais gravando
    # aprovações do MESMO padrão simultaneamente e confirma que NENHUMA
    # se perde (contagem final == N) - também serve de prova de que o
    # lock não trava/faz deadlock (o teste simplesmente não terminaria).
    monkeypatch.setattr(hcf, "CAMINHO_REGISTRO", tmp_path / "historico_centros_funcionarios.json")

    N_THREADS = 25
    erros = []

    def _aprovar(i):
        try:
            hcf.registrar_aprovacao_padrao_documento("rescisao", f"nota {i}")
        except Exception as e:  # nao deve acontecer - qualquer excecao é uma falha do teste
            erros.append(e)

    threads = [threading.Thread(target=_aprovar, args=(i,)) for i in range(N_THREADS)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)  # timeout defensivo - se travar (deadlock), falha em vez de pendurar o teste para sempre

    assert not any(t.is_alive() for t in threads), "uma ou mais threads ainda vivas - indício de deadlock no lock"
    assert not erros, f"threads lançaram exceção inesperada: {erros}"
    assert hcf.contagem_aprovacoes_padrao("rescisao") == N_THREADS
    assert len(hcf.buscar_padrao_documento("rescisao")["notas_diretriz_humana"]) == N_THREADS


def test_registrar_centro_custo_concorrente_nao_perde_atualizacoes(tmp_path, monkeypatch):
    # Mesmo cenário acima, mas para o aprendizado de centro de custo por
    # funcionário (chave diferente do contador de maturação, mesmo
    # arquivo) - N threads confirmando o MESMO funcionário concorrentemente,
    # "vezes_usado" final precisa ser exatamente N (nenhuma gravação perdida).
    monkeypatch.setattr(hcf, "CAMINHO_REGISTRO", tmp_path / "historico_centros_funcionarios.json")

    N_THREADS = 25
    erros = []

    def _registrar(i):
        try:
            hcf.registrar_centro_custo_funcionario("Fulano de Tal", "id-1122", "1122 - Administrativo")
        except Exception as e:
            erros.append(e)

    threads = [threading.Thread(target=_registrar, args=(i,)) for i in range(N_THREADS)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert not any(t.is_alive() for t in threads), "uma ou mais threads ainda vivas - indício de deadlock no lock"
    assert not erros, f"threads lançaram exceção inesperada: {erros}"
    assert hcf.buscar_centro_custo_funcionario("Fulano de Tal")["vezes_usado"] == N_THREADS


def test_diretriz_global_texto_nova_comeca_vazia(tmp_path, monkeypatch):
    monkeypatch.setattr(hcf, "CAMINHO_REGISTRO", tmp_path / "historico_centros_funcionarios.json")

    assert hcf.buscar_diretrizes_globais_texto() == {}


def test_registrar_diretriz_global_texto_grava_e_incrementa(tmp_path, monkeypatch):
    monkeypatch.setattr(hcf, "CAMINHO_REGISTRO", tmp_path / "historico_centros_funcionarios.json")

    hcf.registrar_diretriz_global_texto("voo", categoria_nome="4.15.002. Viagens - A Recuperar")
    entrada = hcf.registrar_diretriz_global_texto("VOO", categoria_nome="4.15.002. Viagens - A Recuperar")

    assert entrada["vezes_confirmada"] == 2
    diretrizes = hcf.buscar_diretrizes_globais_texto()
    assert diretrizes["voo"]["categoria_nome"] == "4.15.002. Viagens - A Recuperar"


def test_registrar_diretriz_global_texto_nao_apaga_campo_nao_informado(tmp_path, monkeypatch):
    monkeypatch.setattr(hcf, "CAMINHO_REGISTRO", tmp_path / "historico_centros_funcionarios.json")

    hcf.registrar_diretriz_global_texto("uber", categoria_nome="4.06.016. Transporte Urbano")
    # Segunda confirmação só informa conta financeira - categoria não pode sumir.
    entrada = hcf.registrar_diretriz_global_texto("uber", conta_financeira_nome="Banco Alfa - Matriz")

    assert entrada["categoria_nome"] == "4.06.016. Transporte Urbano"
    assert entrada["conta_financeira_nome"] == "Banco Alfa - Matriz"


def test_diretrizes_globais_nao_colidem_com_padroes_documento_nem_funcionarios(tmp_path, monkeypatch):
    monkeypatch.setattr(hcf, "CAMINHO_REGISTRO", tmp_path / "historico_centros_funcionarios.json")

    hcf.registrar_centro_custo_funcionario("Fulano de Tal", "id-1", "1121 - Diretoria")
    hcf.registrar_aprovacao_padrao_documento("rescisao")
    hcf.registrar_diretriz_global_texto("hotel", categoria_nome="4.15.002. Viagens - A Recuperar")

    assert hcf.buscar_centro_custo_funcionario("Fulano de Tal")["centro_custo_id"] == "id-1"
    assert hcf.contagem_aprovacoes_padrao("rescisao") == 1
    assert hcf.buscar_diretrizes_globais_texto()["hotel"]["categoria_nome"] == "4.15.002. Viagens - A Recuperar"
