"""Puts the recording `herdr` stub on PATH for the duration of one test."""

import os
import shutil
import tempfile

STUB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "stub")


class StubHerdr:
    def __init__(self, test):
        self.dir = tempfile.mkdtemp(prefix="herdr-stagger-stub-")
        test.addCleanup(shutil.rmtree, self.dir, True)
        self._env(test, "HS_STUB_DIR", self.dir)
        self._env(test, "PATH", STUB_DIR + os.pathsep + os.environ["PATH"])
        self._env(test, "HERDR_ENV", "1")

    def _env(self, test, key, value):
        previous = os.environ.get(key)
        os.environ[key] = value
        test.addCleanup(self._restore, key, previous)

    @staticmethod
    def _restore(key, previous):
        if previous is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = previous

    # --- fixture ------------------------------------------------------------

    def agents(self, agents):
        with open(os.path.join(self.dir, "agents.json"), "w") as handle:
            import json
            json.dump(agents, handle)

    def status(self, pane_id, status):
        self._write("status", pane_id, status)

    def prompt_error(self, pane_id, code):
        self._write("prompt_error", pane_id, code)

    def break_notifications(self):
        open(os.path.join(self.dir, "no_notifications"), "w").close()

    def _write(self, kind, pane_id, value):
        import re
        folder = os.path.join(self.dir, kind)
        os.makedirs(folder, exist_ok=True)
        name = re.sub(r"[^A-Za-z0-9_-]", "-", pane_id)
        with open(os.path.join(folder, name), "w") as handle:
            handle.write(value)

    # --- recording ----------------------------------------------------------

    def calls(self):
        return self._lines("calls")

    def prompts(self):
        return [tuple(line.split("\t", 1)) for line in self._lines("prompts")]

    def _lines(self, name):
        path = os.path.join(self.dir, name)
        if not os.path.exists(path):
            return []
        with open(path) as handle:
            return [line.rstrip("\n") for line in handle if line.strip()]
