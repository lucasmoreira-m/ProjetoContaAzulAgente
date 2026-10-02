"""Secret Manager do Google e travas fora do volume (2026-10-01)."""
import base64
import os

import pytest

from src.core import config
from src.core import conta_azul_auth


class RespostaFalsa:
    def __init__(self, status, corpo=None):
        self.status_code, self._corpo = status, corpo or {}

    def json(self):
        return self._corpo

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class SecretManagerFalso:
    """Guarda versões por segredo, como a API REST do Secret Manager."""

    def __init__(self, segredos):
        self.versoes = {nome: [valor] for nome, valor in segredos.items()}
        self.destruidas = []

    def _nome(self, url):
        return url.split("/secrets/")[1].split("/")[0].split(":")[0]

    def get(self, url, params=None, timeout=None):
        nome = self._nome(url)
        if nome not in self.versoes:
            return RespostaFalsa(404)
        if url.endswith("/versions/latest:access"):
            dado = base64.b64encode(self.versoes[nome][-1].encode()).decode()
            return RespostaFalsa(200, {"payload": {"data": dado}})
        lista = [{"name": f"projects/p/secrets/{nome}/versions/{i}", "createTime": f"2026-10-01T00:00:{i:02d}Z"}
                 for i, _ in enumerate(self.versoes[nome]) if (nome, i) not in self.destruidas]
        return RespostaFalsa(200, {"versions": lista})

    def post(self, url, json=None, timeout=None):
        if url.endswith(":addVersion"):
            self.versoes[self._nome(url)].append(base64.b64decode(json["payload"]["data"]).decode())
        elif url.endswith(":destroy"):
            partes = url.split("/")
            self.destruidas.append((partes[-3], int(partes[-1].split(":")[0])))
        return RespostaFalsa(200)


@pytest.fixture
def secret_manager(monkeypatch):
    falso = SecretManagerFalso({
        "conta-azul-client-id": "cid", "conta-azul-client-secret": "csec",
        "conta-azul-refresh-token": "token-1", "gemini-api-key": "gem", "ms-client-secret": "ms",
    })
    monkeypatch.setattr(config, "GOOGLE_SECRET_MANAGER_PROJETO", "projeto-exemplo")
    monkeypatch.setattr(config, "_sessao_google", lambda: falso)
    for nome in ("CONTA_AZUL_CLIENT_ID", "CONTA_AZUL_CLIENT_SECRET", "CONTA_AZUL_REFRESH_TOKEN",
                 "GEMINI_API_KEY", "MS_CLIENT_SECRET", "SESSAO_CHAVE"):
        monkeypatch.delenv(nome, raising=False)
    return falso


def test_carrega_segredos_obrigatorios_e_opcionais(secret_manager):
    config._carregar_segredos_do_secret_manager()
    assert os.environ["CONTA_AZUL_REFRESH_TOKEN"] == "token-1"
    assert os.environ["GEMINI_API_KEY"] == "gem"
    assert os.environ["MS_CLIENT_SECRET"] == "ms"
    assert "SESSAO_CHAVE" not in os.environ  # opcional ausente: ignorado


def test_segredo_obrigatorio_faltando_da_erro_claro(secret_manager):
    del secret_manager.versoes["gemini-api-key"]
    with pytest.raises(RuntimeError, match="gemini-api-key"):
        config._carregar_segredos_do_secret_manager()


def test_token_renovado_vira_versao_nova_e_antigas_sao_destruidas(secret_manager, monkeypatch):
    monkeypatch.setattr(config, "AZURE_KEY_VAULT_URL", "")
    for n in (2, 3, 4):
        conta_azul_auth._atualizar_refresh_token_no_env(f"token-{n}")
    assert os.environ["CONTA_AZUL_REFRESH_TOKEN"] == "token-4"
    assert secret_manager.versoes["conta-azul-refresh-token"][-1] == "token-4"
    assert config.ler_segredo_google("conta-azul-refresh-token", secret_manager) == "token-4"
    # Ficam só as 2 mais novas habilitadas (o Secret Manager cobra por versão ativa).
    ativas = secret_manager.get(config._url_segredo_google("conta-azul-refresh-token") + "/versions").json()["versions"]
    assert len(ativas) == config.VERSOES_MANTIDAS_SECRET_MANAGER


def test_falha_ao_gravar_no_secret_manager_mantem_token_na_memoria(secret_manager, monkeypatch):
    monkeypatch.setattr(config, "AZURE_KEY_VAULT_URL", "")

    def _quebra(nome, valor):
        raise RuntimeError("sem permissão")

    monkeypatch.setattr(config, "gravar_segredo_google", _quebra)
    conta_azul_auth._atualizar_refresh_token_no_env("token-novo")
    assert os.environ["CONTA_AZUL_REFRESH_TOKEN"] == "token-novo"


def test_trava_fica_ao_lado_do_arquivo_no_pc(monkeypatch):
    monkeypatch.setattr(config, "PASTA_TRAVAS", "")
    assert config.caminho_trava("dados/fornecedores_registry.json") == "dados/fornecedores_registry.json.lock"


def test_trava_vai_para_pasta_local_no_container(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "PASTA_TRAVAS", str(tmp_path / "travas"))
    a = config.caminho_trava("/dados/revisao/x_dados.json")
    b = config.caminho_trava("/dados/processados/x_dados.json")
    assert a.startswith(str(tmp_path / "travas")) and a.endswith("x_dados.json.lock")
    assert a != b  # mesmo nome em pastas diferentes não colide
    assert (tmp_path / "travas").is_dir()


def test_no_google_o_resto_pode_vir_do_secrets_toml(secret_manager, monkeypatch):
    """secrets.toml montado como arquivo + só o refresh_token no Secret Manager."""
    for nome in ("conta-azul-client-id", "conta-azul-client-secret", "gemini-api-key", "ms-client-secret"):
        del secret_manager.versoes[nome]
    monkeypatch.setenv("CONTA_AZUL_CLIENT_ID", "cid-do-toml")
    monkeypatch.setenv("CONTA_AZUL_CLIENT_SECRET", "csec-do-toml")
    monkeypatch.setenv("GEMINI_API_KEY", "gem-do-toml")
    config._carregar_segredos_do_secret_manager()
    assert os.environ["CONTA_AZUL_CLIENT_ID"] == "cid-do-toml"
    assert os.environ["CONTA_AZUL_REFRESH_TOKEN"] == "token-1"  # do Secret Manager


def test_refresh_token_precisa_estar_no_secret_manager_mesmo_com_toml(secret_manager, monkeypatch):
    del secret_manager.versoes["conta-azul-refresh-token"]
    monkeypatch.setenv("CONTA_AZUL_REFRESH_TOKEN", "do-toml-somente-leitura")
    with pytest.raises(RuntimeError, match="conta-azul-refresh-token"):
        config._carregar_segredos_do_secret_manager()
