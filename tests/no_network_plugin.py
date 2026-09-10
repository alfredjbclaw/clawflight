"""Fail any test that opens a network connection.

Enabled in CI with `-p no_network_plugin`. The suite is meant to run with no
network and no credentials, and that claim needs enforcing rather than
asserting: adding a live feed adapter immediately made two CLI tests start
making real requests, and nothing noticed until this ran.
"""
from __future__ import annotations

import socket


class NetworkUsed(RuntimeError):
    """A test tried to use the network."""


def pytest_configure(config):
    def deny(name):
        def _blocked(self, *args, **kwargs):
            raise NetworkUsed(
                "test attempted socket.{}({!r}) — the suite must run offline".format(
                    name, args[0] if args else ""
                )
            )

        return _blocked

    socket.socket.connect = deny("connect")
    socket.socket.connect_ex = deny("connect_ex")
    socket.socket.bind = deny("bind")

    def _no_create_connection(*args, **kwargs):
        raise NetworkUsed("test attempted socket.create_connection")

    socket.create_connection = _no_create_connection
