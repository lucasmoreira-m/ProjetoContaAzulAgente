"""
Helper de upload seguro para o Gemini - resolve um bug real observado em
produção: nomes de arquivo com acentuação (comum em documentos em
português - "Férias", "Rescisão", "Salário", etc.) quebram a codificação
de cabeçalho HTTP usada internamente pela biblioteca `google-genai`:

    UnicodeEncodeError: 'ascii' codec can't encode character '\xe9'
    in position 28: ordinal not in range(128)

O nome do arquivo em si não importa para a extração (o Gemini só processa
o CONTEÚDO do PDF/imagem) - então a correção é simples: se o nome tiver
qualquer caractere fora do ASCII, fazemos uma cópia temporária com nome
seguro só para o upload, e apagamos a cópia depois. O arquivo original em
disco (com o nome real, acentuado) nunca é alterado nem renomeado.
"""

from __future__ import annotations

import contextlib
import os
import shutil
import tempfile
from typing import Iterator


@contextlib.contextmanager
def caminho_seguro_para_upload(caminho_original: str) -> Iterator[str]:
    """Garante um caminho de arquivo com nome ASCII-seguro para passar a
    `client.files.upload(file=...)`. Se o nome original já for seguro,
    devolve o próprio caminho original (sem cópia, sem custo extra). Se
    não for, cria uma cópia temporária com nome seguro, que é apagada
    automaticamente ao sair do bloco `with`."""
    nome_arquivo = os.path.basename(caminho_original)
    try:
        nome_arquivo.encode("ascii")
        yield caminho_original
        return
    except UnicodeEncodeError:
        pass

    _, extensao = os.path.splitext(caminho_original)
    fd, caminho_temp = tempfile.mkstemp(suffix=extensao, prefix="upload_gemini_")
    os.close(fd)
    try:
        shutil.copyfile(caminho_original, caminho_temp)
        yield caminho_temp
    finally:
        if os.path.exists(caminho_temp):
            os.remove(caminho_temp)