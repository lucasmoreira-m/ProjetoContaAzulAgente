"""Credenciais nunca podem voltar a ser embutidas no .exe (2026-09-24):
o antigo env_embutido.py ia como código-fonte legível para
dist/app/_internal/ - qualquer um com a pasta do app lia as chaves."""

from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent


def test_config_nao_carrega_env_embutido():
    codigo = (RAIZ / "src" / "core" / "config.py").read_text(encoding="utf-8")
    assert "import env_embutido" not in codigo
    assert "env_embutido.get_env" not in codigo


def test_nenhum_modulo_do_projeto_importa_env_embutido():
    for arquivo in (RAIZ / "src").rglob("*.py"):
        if arquivo.name == "env_embutido.py":
            continue
        assert "import env_embutido" not in arquivo.read_text(encoding="utf-8"), arquivo


def test_dockerignore_barra_credenciais_e_dados_reais():
    """O "az acr build ." envia a pasta inteira para o Azure e o Dockerfile
    faz COPY . - sem estas linhas, o .env e os PDFs iriam para a imagem."""
    linhas = (RAIZ / ".dockerignore").read_text(encoding="utf-8").splitlines()
    for obrigatoria in (
        ".env", ".env.*", "venv/", "entrada/", "processados/", "revisao/", "descartados/",
        "fornecedores_registry.json", "historico_centros_funcionarios.json", "**/env_embutido.py",
    ):
        assert obrigatoria in linhas, obrigatoria
