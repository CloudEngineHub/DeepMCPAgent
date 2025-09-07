from __future__ import annotations

from deepmcpagent.cli import _merge_servers, _parse_kv


def test_parse_kv_simple() -> None:
    assert _parse_kv(["a=1", "b = two"]) == {"a": "1", "b": "two"}


def test_merge_servers_http_and_stdio() -> None:
    stdios = [
        "name=echo command=python args='-m mypkg.server --port 3333' env.API_KEY=xyz keep_alive=false",
    ]
    https = [
        "name=remote url=https://example.com/mcp transport=http header.Authorization='Bearer abc'",
    ]
    servers = _merge_servers(stdios, https)
    assert set(servers.keys()) == {"echo", "remote"}
    # HTTP server spec
    http = servers["remote"]
    assert http.url == "https://example.com/mcp"
    assert http.headers["Authorization"] == "Bearer abc"
    # stdio server spec
    stdio = servers["echo"]
    assert stdio.command == "python"
    assert stdio.args[0] == "-m"
    assert stdio.keep_alive is False
