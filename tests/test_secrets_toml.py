"""secrets.toml como fonte de credenciais (2026-10-01)."""
import os

import pytest

from src.core import config
from src.core import conta_azul_auth

TOML = '''
# comentário que precisa sobreviver
[conta_azul]
CONTA_AZUL_AMBIENTE = "teste"
CONTA_AZUL_CLIENT_ID = "cid-toml"
CONTA_AZUL_REFRESH_TOKEN = "token-antigo"   # comentário na linha

[microsoft]
MS_CLIENT_ID = ""
USUARIOS_PERMITIDOS = ["a@empresa.com.br", "b@empresa.com.br"]
'''


@pytest.fixture
def toml(tmp_path, monkeypatch):
    caminho = tmp_path / "secrets.toml"
    caminho.write_text(TOML, encoding="utf-8")
    monkeypatch.setattr(config, "CAMINHO_SECRETS_TOML", caminho)
    monkeypatch.setattr(config, "CHAVES_DO_SECRETS_TOML", set())
    monkeypatch.setattr(config, "AZURE_KEY_VAULT_URL", "")
    monkeypatch.setattr(config, "GOOGLE_SECRET_MANAGER_PROJETO", "")
    for nome in ("CONTA_AZUL_AMBIENTE", "CONTA_AZUL_CLIENT_ID", "CONTA_AZUL_REFRESH_TOKEN", "MS_CLIENT_ID",
                 "USUARIOS_PERMITIDOS"):
        monkeypatch.delenv(nome, raising=False)
    return caminho


def test_secoes_viram_variaveis_e_vazio_e_ignorado(toml):
    config._carregar_secrets_toml()
    assert os.environ["CONTA_AZUL_CLIENT_ID"] == "cid-toml"
    assert os.environ["CONTA_AZUL_AMBIENTE"] == "teste"
    assert os.environ["USUARIOS_PERMITIDOS"] == "a@empresa.com.br,b@empresa.com.br"
    assert "MS_CLIENT_ID" not in os.environ  # vazio: deixa o .env completar


def test_variavel_ja_definida_no_ambiente_nao_e_sobrescrita(toml, monkeypatch):
    monkeypatch.setenv("CONTA_AZUL_CLIENT_ID", "do-cloud-run")
    config._carregar_secrets_toml()
    assert os.environ["CONTA_AZUL_CLIENT_ID"] == "do-cloud-run"
    assert "CONTA_AZUL_CLIENT_ID" not in config.CHAVES_DO_SECRETS_TOML


def test_token_renovado_volta_para_o_secrets_toml(toml):
    import tomllib
    config._carregar_secrets_toml()
    conta_azul_auth._atualizar_refresh_token_no_env("token-novo")

    texto = toml.read_text(encoding="utf-8")
    assert tomllib.loads(texto)["conta_azul"]["CONTA_AZUL_REFRESH_TOKEN"] == "token-novo"
    assert "# comentário que precisa sobreviver" in texto
    assert os.environ["CONTA_AZUL_REFRESH_TOKEN"] == "token-novo"


def test_token_que_veio_do_env_continua_indo_para_o_env(toml, monkeypatch, tmp_path):
    """Se o token não estava no secrets.toml, nada muda: vai para o .env."""
    toml.write_text('[conta_azul]\nCONTA_AZUL_CLIENT_ID = "x"\n', encoding="utf-8")
    config._carregar_secrets_toml()
    env = tmp_path / ".env"
    env.write_text("CONTA_AZUL_REFRESH_TOKEN=velho\n", encoding="utf-8")
    monkeypatch.setattr(config, "ENV_FILE_PATH", env)
    conta_azul_auth._atualizar_refresh_token_no_env("novo")
    assert "CONTA_AZUL_REFRESH_TOKEN=novo" in env.read_text(encoding="utf-8")
    assert "CONTA_AZUL_REFRESH_TOKEN" not in toml.read_text(encoding="utf-8")


def test_secrets_toml_nunca_vai_para_git_nem_imagem():
    from pathlib import Path
    raiz = Path(__file__).resolve().parent.parent
    assert "secrets.toml" in (raiz / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert "secrets.toml" in (raiz / ".dockerignore").read_text(encoding="utf-8").splitlines()


def test_aceita_arquivo_com_bom_do_windows(toml):
    toml.write_bytes(b"\xef\xbb\xbf" + TOML.encode("utf-8"))
    config._carregar_secrets_toml()
    assert os.environ["CONTA_AZUL_CLIENT_ID"] == "cid-toml"
