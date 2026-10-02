"""Testes do retry separado leitura/escrita em conta_azul_client.py -
escrita (POST/PUT/PATCH) nunca pode repetir quando a Conta Azul pode já
ter gravado (timeout de leitura, 5xx), senão cria lançamento duplicado."""

import pytest
import requests

from src.core import conta_azul_client as ca


def _retry(sessao):
    return sessao.get_adapter("https://api-v2.contaazul.com").max_retries


class _RespostaFalsa:
    def __init__(self, status_code, corpo=None):
        self.status_code = status_code
        self._corpo = corpo or {}
        self.text = str(self._corpo)
        self.request = type("Req", (), {"method": "POST"})()

    def json(self):
        return self._corpo


def test_escrita_so_repete_em_429_e_nunca_em_timeout_de_leitura():
    retry = _retry(ca._sessao_escrita)
    assert set(retry.status_forcelist) == {429}
    assert retry.read == 0
    assert retry.connect == ca.config.HTTP_MAX_RETRIES
    assert set(retry.allowed_methods) == {"POST", "PUT", "PATCH"}


def test_leitura_continua_repetindo_em_5xx_e_429():
    retry = _retry(ca._sessao)
    assert set(retry.status_forcelist) == {429, 500, 502, 503, 504}
    assert set(retry.allowed_methods) == {"GET"}


@pytest.mark.parametrize("funcao", [ca.post, ca.put, ca.patch])
def test_timeout_de_leitura_em_escrita_marca_resultado_incerto(monkeypatch, funcao):
    def _falha(*a, **k):
        raise requests.ReadTimeout("sem resposta")

    monkeypatch.setattr(ca._sessao_escrita, "request", _falha)
    with pytest.raises(ca.ContaAzulAPIError) as exc:
        funcao("tok", "/v1/x", {})
    assert exc.value.resultado_incerto is True
    assert "PODE ter sido gravado" in str(exc.value)


def test_falha_de_conexao_em_escrita_nao_e_incerta(monkeypatch):
    def _falha(*a, **k):
        raise requests.ConnectionError("recusada")

    monkeypatch.setattr(ca._sessao_escrita, "request", _falha)
    with pytest.raises(ca.ContaAzulAPIError) as exc:
        ca.post("tok", "/v1/x", {})
    assert exc.value.resultado_incerto is False


def test_5xx_em_escrita_marca_resultado_incerto(monkeypatch):
    monkeypatch.setattr(ca._sessao_escrita, "request", lambda *a, **k: _RespostaFalsa(504))
    with pytest.raises(ca.ContaAzulAPIError) as exc:
        ca.post("tok", "/v1/x", {})
    assert exc.value.status_code == 504
    assert exc.value.resultado_incerto is True


def test_4xx_em_escrita_nao_e_incerto(monkeypatch):
    monkeypatch.setattr(ca._sessao_escrita, "request", lambda *a, **k: _RespostaFalsa(400))
    with pytest.raises(ca.ContaAzulAPIError) as exc:
        ca.post("tok", "/v1/x", {})
    assert exc.value.resultado_incerto is False


def test_escrita_ok_devolve_json(monkeypatch):
    monkeypatch.setattr(ca._sessao_escrita, "request", lambda *a, **k: _RespostaFalsa(200, {"id": "abc"}))
    assert ca.post("tok", "/v1/x", {"a": 1}) == {"id": "abc"}
