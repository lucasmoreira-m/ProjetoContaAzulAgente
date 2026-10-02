import threading

from src.utils import verificacao_duplicidade as vd


ITEM_MARIA = {
    "id": "lancamento-maria",
    "total": 1993.0,
    "status": "PENDING",
    "fornecedor": {"id": "id-maria", "nome": "Maria Exemplo Souza"},
    "categorias": [{"id": "id-cat-ferias", "nome": "4.02.004. Férias"}],
}

ITEM_OUTRO_FUNCIONARIO = {
    "id": "lancamento-outro",
    "total": 1993.0,
    "status": "PENDING",
    "fornecedor": {"id": "id-outro", "nome": "Mauricio Xavier Lambertucci"},
    "categorias": [{"id": "id-cat-ferias", "nome": "4.02.004. Férias"}],
}


def _fake_get_dois_funcionarios(access_token, caminho, params=None):
    return {"itens": [ITEM_MARIA, ITEM_OUTRO_FUNCIONARIO]}


def test_mesmo_valor_e_data_mas_contato_diferente_nao_e_duplicata(monkeypatch):
    monkeypatch.setattr(vd.ca, "get", _fake_get_dois_funcionarios)

    resultado = vd.verificar_lancamento_similar_existe(
        "token", 1993.0, "2026-09-28",
        categoria_id="id-cat-ferias", contato_id="id-outro-funcionario-inexistente",
    )
    assert resultado is None


def test_mesmo_contato_categoria_valor_e_data_e_duplicata_por_id(monkeypatch):
    monkeypatch.setattr(vd.ca, "get", _fake_get_dois_funcionarios)

    resultado = vd.verificar_lancamento_similar_existe(
        "token", 1993.0, "2026-09-28",
        categoria_id="id-cat-ferias", contato_id="id-maria",
    )
    assert resultado is ITEM_MARIA


def test_match_por_nome_normalizado_quando_sem_id(monkeypatch):
    monkeypatch.setattr(vd.ca, "get", _fake_get_dois_funcionarios)

    # Nomes exatamente como a Conta Azul devolve (com prefixo de código) -
    # é assim que os 4 chamadores reais passam esses parâmetros (vêm do
    # próprio objeto categoria/contato buscado na API, não de um texto
    # arbitrário), então o match aqui é por igualdade após normalizar.
    resultado = vd.verificar_lancamento_similar_existe(
        "token", 1993.0, "2026-09-28",
        categoria_nome="4.02.004. Férias", contato_nome="MARIA EXEMPLO SOUZA",
    )
    assert resultado is ITEM_MARIA


def test_id_diferente_mas_nome_normalizado_igual_ainda_e_duplicata(monkeypatch):
    # Regressão de um incidente real: a Conta Azul tinha DOIS cadastros de
    # contato diferentes para a mesma "Maria Exemplo Souza" (ids
    # diferentes) - o lançamento antigo usava um id, uma nova extração
    # resolveu o contato para o OUTRO id. Se a checagem exigisse só o id
    # bater, isso deixaria passar uma duplicata de verdade (foi o que
    # aconteceu). Com id E nome sendo alternativas (OR), o nome normalizado
    # ainda pega o caso mesmo com o id "novo" não batendo com o do
    # lançamento antigo.
    monkeypatch.setattr(vd.ca, "get", _fake_get_dois_funcionarios)

    resultado = vd.verificar_lancamento_similar_existe(
        "token", 1993.0, "2026-09-28",
        categoria_id="id-cat-ferias", contato_id="id-maria-cadastro-duplicado-na-conta-azul",
        contato_nome="Maria Exemplo Souza",
    )
    assert resultado is ITEM_MARIA


def test_sem_contato_nem_categoria_informados_usa_so_valor_e_data(monkeypatch):
    def fake_get_um_item(access_token, caminho, params=None):
        return {"itens": [ITEM_MARIA]}

    monkeypatch.setattr(vd.ca, "get", fake_get_um_item)

    resultado = vd.verificar_lancamento_similar_existe("token", 1993.0, "2026-09-28")
    assert resultado is ITEM_MARIA


def test_registrar_arquivo_processado_concorrente_nao_perde_atualizacoes(tmp_path, monkeypatch):
    # Regressão (2026-09-03, risco real apontado em auditoria): mesmo
    # cenário das outras duas suítes de concorrência - N threads
    # registrando N arquivos DIFERENTES (hashes diferentes) simultaneamente
    # no MESMO registro de Camada 1. Todos os N precisam sobreviver no
    # arquivo final (nenhuma gravação perdida), sem deadlock nem exceção.
    monkeypatch.setattr(vd, "CAMINHO_REGISTRO_ARQUIVOS", tmp_path / "arquivos_processados_registry.json")

    N_THREADS = 25
    erros = []

    # Um arquivo físico distinto por thread (conteúdo diferente -> hash
    # diferente) - simula N documentos diferentes chegando ao mesmo tempo,
    # não N tentativas de registrar o MESMO arquivo.
    caminhos = []
    for i in range(N_THREADS):
        caminho = tmp_path / f"documento_{i}.pdf"
        caminho.write_bytes(f"conteudo do documento {i}".encode())
        caminhos.append(str(caminho))

    def _registrar(caminho):
        try:
            vd.registrar_arquivo_processado(caminho, {"sucesso": True, "motivo": "ok", "detalhes": {"protocolo": "123"}})
        except Exception as e:
            erros.append(e)

    threads = [threading.Thread(target=_registrar, args=(c,)) for c in caminhos]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert not any(t.is_alive() for t in threads), "uma ou mais threads ainda vivas - indício de deadlock no lock"
    assert not erros, f"threads lançaram exceção inesperada: {erros}"
    for caminho in caminhos:
        assert vd.verificar_arquivo_ja_processado(caminho) is not None, f"registro de {caminho} foi perdido"
