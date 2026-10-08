"""Contract fixtures forbid provider/AWS network access."""

import socket

import pytest
from support.enterprise import DOMAINS, EnterpriseWorld


@pytest.fixture(autouse=True)
def deny_network(monkeypatch):
    attempts = []

    def denied(*args, **kwargs):
        attempts.append(True)
        raise AssertionError("integration contracts must not access the network")

    monkeypatch.setattr(socket.socket, "connect", denied)
    monkeypatch.setattr(socket.socket, "connect_ex", denied)
    monkeypatch.setattr(socket, "create_connection", denied)
    yield
    assert attempts == [], "network errors must not be hidden by normalized provider failures"


@pytest.fixture(params=DOMAINS)
def domain(request):
    return request.param


@pytest.fixture
def world_factory():
    return EnterpriseWorld


@pytest.fixture
def world(world_factory):
    return world_factory()
