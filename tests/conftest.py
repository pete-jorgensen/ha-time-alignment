"""Home Assistant test harness (pytest-homeassistant-custom-component)."""

import pytest


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    yield


@pytest.fixture
def real_sockets():
    """The HA harness blocks every socket. The protocol tests talk to an in-process
    server over loopback, which is the point of them, so let those through."""
    import pytest_socket

    pytest_socket.enable_socket()
    yield
