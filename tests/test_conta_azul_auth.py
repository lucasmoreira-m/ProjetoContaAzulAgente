import os

from src.core import conta_azul_auth
from src.core import config


def test_cria_env_do_zero_quando_arquivo_nao_existe(tmp_path, monkeypatch):
    caminho_env = tmp_path / "novo.env"
    monkeypatch.setattr(config, "ENV_FILE_PATH", caminho_env)
    monkeypatch.setenv("CONTA_AZUL_CLIENT_ID", "id-teste")
    monkeypatch.setenv("CONTA_AZUL_CLIENT_SECRET", "secret-teste")
    monkeypatch.setenv("CONTA_AZUL_REFRESH_TOKEN", "token-antigo")
    monkeypatch.setenv("GEMINI_API_KEY", "gemini-teste")
    monkeypatch.setenv("CONTA_AZUL_AMBIENTE", "teste")

    assert not caminho_env.exists()

    conta_azul_auth._atualizar_refresh_token_no_env("token-novo")

    assert caminho_env.exists()
    conteudo = caminho_env.read_text(encoding="utf-8")
    assert "CONTA_AZUL_CLIENT_ID=id-teste" in conteudo
    assert "CONTA_AZUL_REFRESH_TOKEN=token-novo" in conteudo
    assert os.environ["CONTA_AZUL_REFRESH_TOKEN"] == "token-novo"


def test_nao_lanca_excecao_quando_arquivo_nao_existe(tmp_path, monkeypatch):
    caminho_env = tmp_path / "novo.env"
    monkeypatch.setattr(config, "ENV_FILE_PATH", caminho_env)
    monkeypatch.setenv("CONTA_AZUL_CLIENT_ID", "id-teste")
    monkeypatch.setenv("CONTA_AZUL_CLIENT_SECRET", "secret-teste")
    monkeypatch.setenv("CONTA_AZUL_REFRESH_TOKEN", "token-antigo")
    monkeypatch.setenv("GEMINI_API_KEY", "gemini-teste")
    monkeypatch.setenv("CONTA_AZUL_AMBIENTE", "teste")

    # Não deve lançar - esse é o comportamento corrigido.
    conta_azul_auth._atualizar_refresh_token_no_env("token-novo")


def test_substitui_refresh_token_quando_arquivo_ja_existe(tmp_path, monkeypatch):
    caminho_env = tmp_path / "existente.env"
    caminho_env.write_text(
        "CONTA_AZUL_CLIENT_ID=id-teste\nCONTA_AZUL_REFRESH_TOKEN=token-antigo\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(config, "ENV_FILE_PATH", caminho_env)

    conta_azul_auth._atualizar_refresh_token_no_env("token-novo")

    conteudo = caminho_env.read_text(encoding="utf-8")
    assert "CONTA_AZUL_REFRESH_TOKEN=token-novo" in conteudo
    assert "token-antigo" not in conteudo
    assert "CONTA_AZUL_CLIENT_ID=id-teste" in conteudo


def test_ainda_lanca_quando_arquivo_existe_mas_falta_a_chave(tmp_path, monkeypatch):
    caminho_env = tmp_path / "existente.env"
    caminho_env.write_text("CONTA_AZUL_CLIENT_ID=id-teste\n", encoding="utf-8")
    monkeypatch.setattr(config, "ENV_FILE_PATH", caminho_env)

    # Proteção deliberada e intencional (não alterada por este fix): não é
    # seguro inserir uma chave nova num arquivo já existente sem ela.
    try:
        conta_azul_auth._atualizar_refresh_token_no_env("token-novo")
        assert False, "deveria ter lançado RuntimeError"
    except RuntimeError:
        pass


class _KeyVaultFalso:
    def __init__(self, falhar: bool = False):
        self.gravados = {}
        self.falhar = falhar

    def set_secret(self, nome, valor):
        if self.falhar:
            raise PermissionError("Forbidden")
        self.gravados[nome] = valor


def test_no_azure_grava_refresh_token_no_key_vault_e_nao_no_env(tmp_path, monkeypatch):
    caminho_env = tmp_path / "nao_deve_existir.env"
    kv = _KeyVaultFalso()
    monkeypatch.setattr(config, "ENV_FILE_PATH", caminho_env)
    monkeypatch.setattr(config, "AZURE_KEY_VAULT_URL", "https://seu-keyvault.vault.azure.net/")
    monkeypatch.setattr(config, "cliente_key_vault", lambda: kv)
    monkeypatch.setenv("CONTA_AZUL_REFRESH_TOKEN", "token-antigo")

    conta_azul_auth._atualizar_refresh_token_no_env("token-novo")

    assert kv.gravados == {"conta-azul-refresh-token": "token-novo"}
    assert os.environ["CONTA_AZUL_REFRESH_TOKEN"] == "token-novo"
    assert not caminho_env.exists()


def test_no_azure_falha_no_key_vault_mantem_token_novo_em_memoria(monkeypatch):
    monkeypatch.setattr(config, "AZURE_KEY_VAULT_URL", "https://seu-keyvault.vault.azure.net/")
    monkeypatch.setattr(config, "cliente_key_vault", lambda: _KeyVaultFalso(falhar=True))
    monkeypatch.setenv("CONTA_AZUL_REFRESH_TOKEN", "token-antigo")

    # Não lança: o token antigo já foi invalidado, o processo segue com o novo.
    conta_azul_auth._atualizar_refresh_token_no_env("token-novo")

    assert os.environ["CONTA_AZUL_REFRESH_TOKEN"] == "token-novo"
