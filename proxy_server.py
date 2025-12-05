import argparse
import socket
import threading
from dataclasses import dataclass
from typing import Dict, Tuple


@dataclass
class NATEntry:
    client_conn: socket.socket
    client_addr: str
    client_port: int


class ProxyServer:
    def __init__(self, listen_host: str, listen_port: int, server_host: str, server_port: int) -> None:
        self.listen_host = listen_host
        self.listen_port = listen_port
        self.server_host = server_host
        self.server_port = server_port
        self.nat_table: Dict[int, NATEntry] = {}
        self.nat_lock = threading.Lock()

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
                        print(
                            f"NAT: {client_addr[0]}:{client_addr[1]} -> "
                            f"{self.listen_host}:{nat_port} -> {self.server_host}:{self.server_port} | "
                            f"cmd={request_line}"
                        )

                        outbound = request_line if request_line.endswith("\n") else request_line + "\n"
                        server_sock.sendall(outbound.encode("utf-8"))

                        self._forward_response(server_sock, nat_port, outbound)
                except Exception as exc:  # noqa: BLE001
                    error_message = f"proxy error: {exc}"
                    if nat_port is not None:
                        self._send_err_via_nat(nat_port, error_message)
                    else:
                        client_conn.sendall(f"ERR {error_message}\n".encode("utf-8"))
                finally:
                    if nat_port is not None:
                        self._remove_nat(nat_port)

            self._remove_nat_by_client(client_addr)

    def _forward_response(self, server_sock: socket.socket, nat_port: int, request_line: str) -> None:
        """
        Reverse-proxy style: look up the client using the NAT table and send responses
        back through that mapping instead of holding direct references.
        """
        response_line = self._readline(server_sock)
        if not response_line:
            self._send_err_via_nat(nat_port, "empty response from server")
            return

        self._send_line_via_nat(nat_port, response_line)

        if response_line.startswith("OK ") and request_line.strip().upper().startswith("DOWNLOAD"):
            try:
                _, size_str = response_line.split(maxsplit=1)
                remaining = int(size_str)
            except ValueError:
                self._send_err_via_nat(nat_port, "invalid size from server")
                return
            self._relay_bytes(server_sock, nat_port, remaining)
        elif response_line == "OK" and request_line.strip().upper() == "LIST":
            self._relay_until_end(server_sock, nat_port)

    def _relay_bytes(self, server_sock: socket.socket, nat_port: int, remaining: int) -> None:
        while remaining > 0:
            chunk = server_sock.recv(min(4096, remaining))
            if not chunk:
                break
            remaining -= len(chunk)
            self._send_raw_via_nat(nat_port, chunk)

    def _relay_until_end(self, server_sock: socket.socket, nat_port: int) -> None:
        # For LIST responses: pass through lines until END marker.
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

    def _remove_nat_by_client(self, client_addr: Tuple[str, int]) -> None:
        with self.nat_lock:
            to_delete = [
                port
                for port, entry in self.nat_table.items()
                if (entry.client_addr, entry.client_port) == client_addr
            ]
            for port in to_delete:
                del self.nat_table[port]

    def _remove_nat(self, nat_port: int) -> None:
        with self.nat_lock:
            self.nat_table.pop(nat_port, None)

    def _send_raw_via_nat(self, nat_port: int, payload: bytes) -> None:
        entry = self._get_nat_entry(nat_port)
        if entry is None:
            return
        entry.client_conn.sendall(payload)

    def _send_line_via_nat(self, nat_port: int, line: str) -> None:
        if not line.endswith("\n"):
            line = line + "\n"
        self._send_raw_via_nat(nat_port, line.encode("utf-8"))

    def _send_err_via_nat(self, nat_port: int, message: str) -> None:
        self._send_line_via_nat(nat_port, f"ERR {message}")

    def _get_nat_entry(self, nat_port: int) -> NATEntry | None:
        with self.nat_lock:
            return self.nat_table.get(nat_port)

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


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Threaded NAT/PAT proxy for the file server.")
    parser.add_argument("--listen-host", default="0.0.0.0", help="Proxy bind address.")
    parser.add_argument("--listen-port", type=int, default=9000, help="Proxy listen port.")
    parser.add_argument("--server-host", default="127.0.0.1", help="Upstream file server host.")
    parser.add_argument("--server-port", type=int, default=9001, help="Upstream file server port.")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    proxy = ProxyServer(args.listen_host, args.listen_port, args.server_host, args.server_port)
    proxy.start()


if __name__ == "__main__":
    main()
