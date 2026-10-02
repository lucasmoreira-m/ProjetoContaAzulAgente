from pathlib import Path

from src.utils import backup_registros


def test_faz_backup_em_subpasta_irma(tmp_path):
    original = tmp_path / "fornecedores_registry.json"
    original.write_text('{"a": 1}', encoding="utf-8")

    backup_registros.fazer_backup_com_rotacao(original)

    pasta_backups = tmp_path / "backups" / "fornecedores_registry"
    backups = list(pasta_backups.glob("*.json"))
    assert len(backups) == 1
    assert backups[0].read_text(encoding="utf-8") == '{"a": 1}'


def test_nao_faz_nada_se_arquivo_original_nao_existe(tmp_path):
    original = tmp_path / "nao_existe.json"

    backup_registros.fazer_backup_com_rotacao(original)

    assert not (tmp_path / "backups").exists()


def test_rotacao_mantem_so_os_ultimos_n_backups(tmp_path):
    original = tmp_path / "historico_centros_funcionarios.json"

    for i in range(5):
        original.write_text(f'{{"versao": {i}}}', encoding="utf-8")
        backup_registros.fazer_backup_com_rotacao(original, manter_ultimos=3)

    pasta_backups = tmp_path / "backups" / "historico_centros_funcionarios"
    backups = sorted(pasta_backups.glob("*.json"))
    assert len(backups) == 3
    # As 3 mantidas são as 3 últimas gravações (versao 2, 3, 4) - a mais
    # recente (ordenação por nome, que embute o timestamp) tem a versão 4.
    assert '"versao": 4' in backups[-1].read_text(encoding="utf-8")


def test_manter_ultimos_zero_desliga_rotacao(tmp_path):
    original = tmp_path / "fornecedores_registry.json"

    for i in range(4):
        original.write_text(f'{{"versao": {i}}}', encoding="utf-8")
        backup_registros.fazer_backup_com_rotacao(original, manter_ultimos=0)

    pasta_backups = tmp_path / "backups" / "fornecedores_registry"
    assert len(list(pasta_backups.glob("*.json"))) == 4


def test_falha_ao_copiar_nao_lanca_excecao(tmp_path, monkeypatch):
    original = tmp_path / "fornecedores_registry.json"
    original.write_text("{}", encoding="utf-8")

    def _explode(*args, **kwargs):
        raise OSError("disco cheio simulado")

    monkeypatch.setattr(backup_registros.shutil, "copy2", _explode)

    # Não deve lançar exceção nenhuma - o dado principal já foi salvo antes
    # desta chamada, uma falha de backup não pode travar o fluxo.
    backup_registros.fazer_backup_com_rotacao(original)
