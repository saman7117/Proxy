import socketserver
from pathlib import Path


class FileRequestHandler(socketserver.BaseRequestHandler):
    """
    Simple file server protocol:
      - Client sends a single line command terminated by '\\n':
          LIST
          DOWNLOAD <filename>
      - Responses:
          LIST success:  "OK\\n" followed by newline-separated filenames then "END\\n"
          DOWNLOAD success: "OK <size>\\n" followed by raw file bytes
          Error: "ERR <message>\\n"
    """

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

    def _handle_list(self) -> None:
        files = []
        for path in self.server.files_dir.iterdir():  # type: ignore[attr-defined]
            if path.is_file():
                files.append(path.name)
        response = "OK\n" + "\n".join(files) + "\nEND\n"
        self.request.sendall(response.encode("utf-8"))

    def _handle_download(self, filename: str) -> None:
        file_path = self.server.files_dir / filename  # type: ignore[attr-defined]
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
        server.files_dir = files_dir  # type: ignore[attr-defined]
        print(f"File server listening on {host}:{port}, serving files from {files_dir}")
        server.serve_forever()


if __name__ == "__main__":
    # Hardcoded configuration
    HOST = "127.0.0.1"
    PORT = 9001
    FILES_DIR = Path("files")

    run_server(HOST, PORT, FILES_DIR)
