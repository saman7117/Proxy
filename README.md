# Proxy + File Server Demo

This project is a tiny, thread-safe NAT/PAT-style proxy that sits in front of a simple file server. A lightweight client connects to the proxy, which forwards commands to the upstream server and relays responses back. This doc introduces the pieces, shows how they connect (ports, flow), and lists the commands you can run.

## Components at a Glance

- `file_server.py`: threaded TCP file server that exposes a minimal text protocol for listing and downloading files.
- `proxy_server.py`: TCP proxy that accepts client commands, registers a NAT entry, forwards the request to the file server, and streams the response back.
- `client.py`: CLI helper that speaks the same protocol and provides `list` and `download` commands interactively.

## Connections and Ports

- Hardcoded ports in code:
  - Proxy listener: `0.0.0.0:9000` (forwards to the file server)
  - File server: `127.0.0.1:9001` (serves files from `./files`)
- Typical flow: `client -> proxy (0.0.0.0:9000) -> file server (127.0.0.1:9001)`
- To change ports/hosts, edit the constants in `proxy_server.py` and `file_server.py`.

Start everything (no CLI flags needed):
```bash
# File server
python file_server.py

# Proxy (in another shell)
python proxy_server.py
```

Inside the proxy, each inbound client connection is mapped to a temporary NAT entry so responses can be routed back correctly:
```python
# proxy_server.py
nat_port = server_sock.getsockname()[1]
self._register_nat(nat_port, client_conn, client_addr)
```

## Protocol Overview

The file server accepts one-line, newline-terminated commands:

```text
LIST
DOWNLOAD <filename>
```

Responses (from `file_server.py`):
```python
"""
LIST success:      "OK\n" followed by newline-separated filenames then "END\n"
DOWNLOAD success:  "OK <size>\n" followed by raw file bytes
Error:             "ERR <message>\n"
"""
```

The proxy preserves this protocol. For downloads, it streams exactly the number of bytes advertised in the `OK <size>` header back to the client. For listings, it relays lines until it sees the `END` marker.

## Commands You Can Run

Use the provided client to speak the protocol through the proxy:

```bash
# Connect to the proxy (hardcoded 127.0.0.1:9000)
python client.py
```

Interactive prompt commands (`proxy>`):
- `list` — fetch and print the server-side filenames.
- `download <filename> [dest]` — download a file to `dest` (or the same name in the current directory if omitted).
- `exit` / `quit` — close the session.

If you prefer to script directly without the helper, open a TCP connection to the proxy and send the protocol lines yourself—everything is plain text except the raw file bytes that follow a successful download header.

## What This Documentation Covers Next

This doc is structured to guide you in order:
1) Introduction and component roles (above)
2) Protocol and connection details (ports, flow, and wire format)
3) Commands and examples (just covered)

From here you can extend the docs with deployment notes, security considerations, or testing steps as needed.
