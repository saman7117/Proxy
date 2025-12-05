import argparse
import shlex
import socket
from pathlib import Path
from typing import List


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
    try:
        _, size_str = header.split(maxsplit=1)
        remaining = int(size_str)
    except ValueError:
        raise RuntimeError(f"Invalid size in response: {header}")

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


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Client for the NAT/PAT proxy demo.")
    parser.add_argument("--proxy-host", default="127.0.0.1", help="Proxy server host.")
    parser.add_argument("--proxy-port", type=int, default=9000, help="Proxy server port.")
    subparsers = parser.add_subparsers(dest="command")

    subparsers.add_parser("list", help="Run a single list command before entering the prompt.")

    download_parser = subparsers.add_parser(
        "download", help="Run a single download command before entering the prompt."
    )
    download_parser.add_argument("filename", help="Name of the file to download.")
    download_parser.add_argument(
        "--out",
        type=Path,
        help="Destination path for the downloaded file (default: same name in current directory).",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    with socket.create_connection((args.proxy_host, args.proxy_port)) as sock:
        print(f"Connected to proxy at {args.proxy_host}:{args.proxy_port}")

        # Optional initial command for compatibility with previous CLI.
        if args.command == "list":
            _handle_list(sock)
        elif args.command == "download":
            destination = args.out or Path(args.filename)
            _handle_download(sock, args.filename, destination)

        _interactive_loop(sock)


def _handle_list(sock: socket.socket) -> None:
    files = list_files(sock)
    print("Files on server:")
    for name in files:
        print(f"- {name}")


def _handle_download(sock: socket.socket, filename: str, destination: Path) -> None:
    download_file(sock, filename, destination)
    print(f"Downloaded to {destination}")


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

        try:
            parts = shlex.split(raw)
        except ValueError as exc:
            print(f"Could not parse command: {exc}")
            continue
        if not parts:
            continue

        cmd = parts[0].lower()
        try:
            if cmd == "list":
                _handle_list(sock)
            elif cmd == "download" and len(parts) >= 2:
                destination = Path(parts[2]) if len(parts) >= 3 else Path(parts[1])
                _handle_download(sock, parts[1], destination)
            else:
                print('Unknown command. Use "list", "download <filename>", or "exit".')
        except Exception as exc:  # noqa: BLE001
            print(f"Command failed: {exc}")


if __name__ == "__main__":
    main()
