"""A request-driven task whose progress lives only in process memory."""

import json
import os
import uuid
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

VALUES = [11, 23, 37, 41, 59]


class Task:
    def __init__(self, root):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.token = uuid.uuid4().hex
        self.index = 0
        self.total = 0

    def state(self):
        return {"token": self.token, "index": self.index, "total": self.total}

    def step(self):
        if self.index < len(VALUES):
            self.total += VALUES[self.index]
            self.index += 1
        if self.index == len(VALUES):
            (self.root / "result.json").write_text(
                json.dumps({"count": self.index, "sum": self.total}) + "\n"
            )
        return self.state()


def serve():
    task = Task(os.environ.get("TASK_ROOT", "/tmp/lifecycle-task"))

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path != "/state":
                self.send_error(404)
                return
            self.reply(task.state())

        def do_POST(self):
            if self.path != "/step":
                self.send_error(404)
                return
            self.reply(task.step())

        def reply(self, state):
            body = json.dumps(state).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_):
            pass

    HTTPServer(("127.0.0.1", 8765), Handler).serve_forever()


if __name__ == "__main__":
    serve()
