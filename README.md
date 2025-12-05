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

Start everything:

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

## Function-by-Function Notes with Snippets

### file_server.py

**Request dispatch**

```python
class FileRequestHandler(socketserver.BaseRequestHandler):
    def handle(self) -> None:
        line = self._readline()
        if not line:
            return
        parts = line.strip().split(maxsplit=1)
        command = parts[0].upper()

        if command == "LIST":
            self._handle_list()
        elif command == "DOWNLOAD" and len(parts) == 2:
            self._handle_download(parts[1])
        else:
            self._send_err("unknown command")
```

Reads exactly one newline-terminated command from the TCP socket, strips it, uppercases the verb, and dispatches. Guardrails: ignores blank lines, rejects unknown verbs, and responds with a protocol `ERR` rather than throwing—this keeps the connection alive and predictable for clients.

**List files**

```python
def _handle_list(self) -> None:
    files = []
    for path in self.server.files_dir.iterdir():
        if path.is_file():
            files.append(path.name)
    response = "OK\n" + "\n".join(files) + "\nEND\n"
    self.request.sendall(response.encode("utf-8"))
```

Walks the configured `files_dir`, filters out non-files (so directory names or sockets don’t leak), builds a deterministic response (`OK`, filenames, `END`). The explicit `END` marker is crucial because TCP is stream-based—without it the client wouldn’t know when the list ends.

**Download file**

```python
def _handle_download(self, filename: str) -> None:
    file_path = self.server.files_dir / filename
    if not file_path.exists() or not file_path.is_file():
        self._send_err("file not found")
        return

    size = file_path.stat().st_size
    header = f"OK {size}\n"
    self.request.sendall(header.encode("utf-8"))
    with file_path.open("rb") as f:
        while True:
            chunk = f.read(4096)
            if not chunk:
                break
            self.request.sendall(chunk)
```

Confirms the target is an existing regular file. Sends `OK <size>` so the client knows exactly how many bytes to expect, then streams in 4 KiB chunks to keep memory bounded even for large files. Any missing/invalid path yields a protocol `ERR file not found`—no stack traces or partial data.

**Helpers and server bootstrap**

```python
def _send_err(self, message: str) -> None:
    self.request.sendall(f"ERR {message}\n".encode("utf-8"))

def _readline(self) -> str | None:
    buffer = bytearray()
    while True:
        data = self.request.recv(1)
        if not data:
            return None
        if data == b"\n":
            break
        buffer.extend(data)
    return buffer.decode("utf-8")

def run_server(host: str, port: int, files_dir: Path) -> None:
    files_dir.mkdir(parents=True, exist_ok=True)
    handler = FileRequestHandler
    with socketserver.ThreadingTCPServer((host, port), handler) as server:
        server.allow_reuse_address = True
        server.files_dir = files_dir
        print(f"File server listening on {host}:{port}, serving files from {files_dir}")
        server.serve_forever()
```

`_send_err` centralizes error formatting so every failure uses the same wire format. `_readline` consumes one byte at a time to gracefully handle packet fragmentation on TCP. `run_server` pre-creates the serving directory, spins up a threaded TCP server (one thread per client), stashes `files_dir` on the server object for handlers to use, and enables `SO_REUSEADDR` to restart cleanly.

### proxy_server.py

**Start listener**

```python
def start(self) -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind((self.listen_host, self.listen_port))
        listener.listen()
        print(f"Proxy listening on {self.listen_host}:{self.listen_port}")
        print(f"Forwarding to file server at {self.server_host}:{self.server_port}")

        while True:
            client_conn, client_addr = listener.accept()
            thread = threading.Thread(
                target=self.handle_client, args=(client_conn, client_addr), daemon=True
            )
            thread.start()
```

Configures `SO_REUSEADDR` to allow fast restarts, binds, listens, and for each client spawns a daemon thread that handles that connection independently. This keeps the accept loop free to serve new clients while existing ones are processing downloads/lists.

**Handle a client request**

```python
def handle_client(self, client_conn: socket.socket, client_addr: Tuple[str, int]) -> None:
    with client_conn:
        while True:
            request_line = self._readline(client_conn)
            if request_line is None:
                break
            request_line = request_line.strip()
            if not request_line:
                continue

            nat_port = None
            try:
                with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server_sock:
                    server_sock.connect((self.server_host, self.server_port))
                    nat_port = server_sock.getsockname()[1]
                    self._register_nat(nat_port, client_conn, client_addr)
                    outbound = request_line if request_line.endswith("\n") else request_line + "\n"
                    server_sock.sendall(outbound.encode("utf-8"))
                    self._forward_response(server_sock, nat_port, outbound)
            except Exception as exc:
                error_message = f"proxy error: {exc}"
                if nat_port is not None:
                    self._send_err_via_nat(nat_port, error_message)
                else:
                    client_conn.sendall(f"ERR {error_message}\n".encode("utf-8"))
            finally:
                if nat_port is not None:
                    self._remove_nat(nat_port)

        self._remove_nat_by_client(client_addr)
```

Implements a tiny reverse-proxy/NAT loop. For every incoming command it:

1. opens a fresh upstream socket to the file server (avoids stale state),
2. registers a NAT entry keyed by the upstream local port so return traffic can be mapped to the originating client,
3. normalizes and forwards the command,
4. streams the upstream response back through the NAT lookup,
5. unregisters the NAT entry on success or error.
   All exceptions are translated into `ERR proxy error: ...` so the client gets feedback instead of a silent drop.

**Forward responses**

```python
def _forward_response(self, server_sock: socket.socket, nat_port: int, request_line: str) -> None:
    response_line = self._readline(server_sock)
    if not response_line:
        self._send_err_via_nat(nat_port, "empty response from server")
        return

    self._send_line_via_nat(nat_port, response_line)

    if response_line.startswith("OK ") and request_line.strip().upper().startswith("DOWNLOAD"):
        _, size_str = response_line.split(maxsplit=1)
        remaining = int(size_str)
        self._relay_bytes(server_sock, nat_port, remaining)
    elif response_line == "OK" and request_line.strip().upper() == "LIST":
        self._relay_until_end(server_sock, nat_port)
```

Pulls the first upstream line, immediately forwards it, then branches by verb: `DOWNLOAD` triggers a size parse and a counted byte relay (no over/under-run), `LIST` triggers a line-by-line relay until `END`. If the upstream dies early, an error is injected back to the client instead of hanging.

**Streaming and NAT helpers**

```python
def _relay_bytes(self, server_sock: socket.socket, nat_port: int, remaining: int) -> None:
    while remaining > 0:
        chunk = server_sock.recv(min(4096, remaining))
        if not chunk:
            break
        remaining -= len(chunk)
        self._send_raw_via_nat(nat_port, chunk)

def _relay_until_end(self, server_sock: socket.socket, nat_port: int) -> None:
    while True:
        line = self._readline(server_sock)
        if line is None:
            break
        self._send_line_via_nat(nat_port, line)
        if line == "END":
            break

def _register_nat(self, nat_port: int, client_conn: socket.socket, client_addr: Tuple[str, int]) -> None:
    with self.nat_lock:
        self.nat_table[nat_port] = NATEntry(client_conn, client_addr[0], client_addr[1])
```

`_relay_bytes` streams a fixed byte count without buffering more than 4 KiB at a time; `_relay_until_end` streams until a protocol sentinel. NAT table writes/reads are mutex-protected so concurrent client threads don’t race when registering or cleaning up entries keyed by the ephemeral upstream port.

**Write helpers and reader**

```python
def _send_line_via_nat(self, nat_port: int, line: str) -> None:
    if not line.endswith("\n"):
        line = line + "\n"
    self._send_raw_via_nat(nat_port, line.encode("utf-8"))

def _readline(self, sock: socket.socket) -> str | None:
    buffer = bytearray()
    while True:
        data = sock.recv(1)
        if not data:
            if not buffer:
                return None
            break
        if data == b"\n":
            break
        buffer.extend(data)
    return buffer.decode("utf-8")
```

Ensures every line sent via NAT ends with `\n` (clients expect line-delimited messages). `_readline` is the same conservative reader used everywhere, giving symmetric parsing for client- and server-facing sockets even when TCP delivers partial frames.

### client.py

**Protocol helpers**

```python
def send_line(sock: socket.socket, command: str) -> None:
    if not command.endswith("\n"):
        command += "\n"
    sock.sendall(command.encode("utf-8"))

def readline(sock: socket.socket) -> str | None:
    buffer = bytearray()
    while True:
        data = sock.recv(1)
        if not data:
            if not buffer:
                return None
            break
        if data == b"\n":
            break
        buffer.extend(data)
    return buffer.decode("utf-8")
```

Tiny utilities that enforce newline termination and incremental line reads, mirroring the server/proxy rules so higher-level helpers don’t need to worry about framing or partial TCP reads.

**LIST and DOWNLOAD**

```python
def list_files(sock: socket.socket) -> List[str]:
    send_line(sock, "LIST")
    header = readline(sock)
    if header != "OK":
        raise RuntimeError(f"Unexpected response: {header}")
    files = []
    while True:
        line = readline(sock)
        if line is None:
            break
        if line == "END":
            break
        files.append(line)
    return files

def download_file(sock: socket.socket, filename: str, dest: Path) -> None:
    send_line(sock, f"DOWNLOAD {filename}")
    header = readline(sock)
    if not header or not header.startswith("OK "):
        raise RuntimeError(f"Unexpected response: {header}")
    _, size_str = header.split(maxsplit=1)
    remaining = int(size_str)

    dest.parent.mkdir(parents=True, exist_ok=True)
    with dest.open("wb") as f:
        while remaining > 0:
            chunk = sock.recv(min(4096, remaining))
            if not chunk:
                break
            f.write(chunk)
            remaining -= len(chunk)
    if remaining != 0:
        raise RuntimeError("Download incomplete")
```

Implements the client side of LIST/DOWNLOAD: issues the command, asserts the header is the expected `OK` form, parses sizes, and streams data directly to disk. Creates parent directories on demand and verifies the received byte count matches what the server promised, failing fast if the transfer is short.

**Entry point and REPL**

```python
def main() -> None:
    PROXY_HOST = "127.0.0.1"
    PROXY_PORT = 9000

    with socket.create_connection((PROXY_HOST, PROXY_PORT)) as sock:
        print(f"Connected to proxy at {PROXY_HOST}:{PROXY_PORT}")
        _interactive_loop(sock)

def _interactive_loop(sock: socket.socket) -> None:
    print('Enter commands: "list", "download <filename>", or "exit".')
    while True:
        try:
            raw = input("proxy> ").strip()
        except EOFError:
            print()
            break
        if not raw:
            continue
        if raw.lower() in {"exit", "quit"}:
            break

        parts = shlex.split(raw)
        if not parts:
            continue

        cmd = parts[0].lower()
        if cmd == "list":
            _handle_list(sock)
        elif cmd == "download" and len(parts) >= 2:
            destination = Path(parts[2]) if len(parts) >= 3 else Path(parts[1])
            _handle_download(sock, parts[1], destination)
        else:
            print('Unknown command. Use "list", "download <filename>", or "exit".')
```

Connects to the proxy at the hardcoded endpoint and enters a REPL. `shlex.split` allows filenames with spaces/quotes. Commands dispatch to the protocol helpers; exceptions are caught and printed so a bad request doesn’t kill the session. Unknown commands just print guidance and keep the prompt running.

## NAT Table Data Structure and Threading

### NAT Table

**Shape and purpose**

```python
@dataclass
class NATEntry:
    client_conn: socket.socket
    client_addr: str
    client_port: int

class ProxyServer:
    def __init__(...):
        self.nat_table: Dict[int, NATEntry] = {}
        self.nat_lock = threading.Lock()
```

`nat_table` is a dictionary keyed by the upstream socket’s local port (an ephemeral port assigned when the proxy connects to the file server). Each entry stores the client socket plus the client’s IP/port so responses can be routed back. A single `threading.Lock` (`nat_lock`) guards all reads/writes to keep the dict consistent across threads.

**Lifecycle: register → use → remove**

```python
def _register_nat(self, nat_port: int, client_conn: socket.socket, client_addr: Tuple[str, int]) -> None:
    with self.nat_lock:
        self.nat_table[nat_port] = NATEntry(client_conn, client_addr[0], client_addr[1])

def _remove_nat(self, nat_port: int) -> None:
    with self.nat_lock:
        self.nat_table.pop(nat_port, None)

def _remove_nat_by_client(self, client_addr: Tuple[str, int]) -> None:
    with self.nat_lock:
        to_delete = [
            port
            for port, entry in self.nat_table.items()
            if (entry.client_addr, entry.client_port) == client_addr
        ]
        for port in to_delete:
            del self.nat_table[port]
```

Writes (register/remove) happen under the lock to avoid races between worker threads. `_remove_nat_by_client` is a cleanup pass invoked when a client disconnects; it scans for any lingering mappings tied to that client and removes them.

**Lookups and forwarding**

```python
def _get_nat_entry(self, nat_port: int) -> NATEntry | None:
    with self.nat_lock:
        return self.nat_table.get(nat_port)

def _send_raw_via_nat(self, nat_port: int, payload: bytes) -> None:
    entry = self._get_nat_entry(nat_port)
    if entry is None:
        return
    entry.client_conn.sendall(payload)
```

Reads also take the lock to fetch a consistent view of the mapping; if a mapping vanished mid-flight, the send is skipped instead of crashing. All relay helpers (`_send_line_via_nat`, `_send_err_via_nat`) go through this path so every downstream write is NAT-checked.

**Example mapping**

| nat_table key (nat_port) | client_addr | client_port | client_conn (socket) |
| ------------------------ | ----------- | ----------- | -------------------- |
| 52034                    | 10.0.0.5    | 43210       | <socket to client A> |
| 52035                    | 10.0.0.6    | 43211       | <socket to client B> |

Flow for client A running `DOWNLOAD foo.txt`:

1. Proxy opens upstream socket to file server; OS assigns local port `52034`.
2. `_register_nat(52034, client_conn_A, ("10.0.0.5", 43210))`.
3. Command forwarded upstream; responses are pulled from upstream and sent via `_send_*_via_nat(52034, ...)`.
4. When the command finishes (or errors), `_remove_nat(52034)` cleans the entry. If the client disconnects unexpectedly, `_remove_nat_by_client` removes any entries for that client.

### Threading Model

**What threads exist**

- Listener thread (main thread inside `start`): accepts clients and spawns handler threads.
- One handler thread per accepted client: runs `handle_client`, processes that client’s commands sequentially.
- Upstream sockets are short-lived and created inside the handler thread (one per command); there is no thread per upstream connection.

**Why this works**

- Each client has isolated control flow, so a slow download doesn’t block other clients.
- Shared mutable state (the NAT table) is protected by `nat_lock`, eliminating races when multiple handler threads add/remove/look up entries concurrently.
- Sockets are per-thread objects (except client sockets stored in NAT entries), so there is no cross-thread socket sharing without protection.

**How threads end**

- Handler thread loop exits when `_readline(client_conn)` returns `None` (client closed) or when an unrecoverable error breaks the loop.
- On exit, `_remove_nat_by_client` runs to purge any mappings belonging to that client.
- Threads are started as `daemon=True`, so they do not prevent process shutdown; when the main thread exits, daemon handler threads are terminated automatically.

**Thread-safe cleanup and failure paths**

- During normal completion of a command: `_remove_nat` clears the ephemeral NAT mapping created for that upstream socket.
- On exceptions: the `finally` block still calls `_remove_nat` if a mapping was registered, ensuring the table doesn’t retain stale entries.
- If a client disconnects mid-command: `_readline` returns `None`, loop breaks, `_remove_nat_by_client` clears any leftovers.

**Concurrency summary**

- Work unit: one client connection = one handler thread.
- Shared state: `nat_table`, protected by `nat_lock` for all mutations and lookups.
- Data flow: per-command upstream sockets use OS-assigned ephemeral ports; these ports are the keys to route responses back to the correct client socket stored in the NAT table.
- Shutdown: daemon threads allow clean process exit; explicit NAT cleanup prevents dangling mappings even under errors.
