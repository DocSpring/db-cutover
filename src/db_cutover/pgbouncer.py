"""PgBouncer pods in Kubernetes: an admin console connection to each pod through `kubectl port-forward`.

Port-forwarding keeps the admin password out of every command line (no `kubectl exec ... PGPASSWORD=...`), and a
persistent connection per pod makes PAUSE / RELOAD / RESUME take milliseconds instead of a kubectl round trip each.
"""

from __future__ import annotations

import concurrent.futures
import contextlib
import json
import queue
import re
import subprocess
import threading
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import psycopg
from psycopg.rows import dict_row

from db_cutover.config import Config
from db_cutover.output import error_detail
from db_cutover.shell import kubectl


@dataclass
class Pod:
    name: str
    ready: bool
    port_forward: subprocess.Popen | None = None
    local_port: int | None = None
    conn: psycopg.Connection | None = None
    spare: psycopg.Connection | None = None  # for RESUME while `conn` is stuck in a PAUSE

    def admin(self, command: str) -> list[dict[str, Any]]:
        assert self.conn is not None, f"{self.name} is not connected"
        cur = self.conn.execute(command)
        return cur.fetchall() if cur.description else []


def parse_database_host(ini: str, database: str) -> str | None:
    """The host of `database` in a pgbouncer.ini [databases] section."""
    in_databases = False
    for raw in ini.splitlines():
        line = raw.strip()
        if line.startswith("["):
            in_databases = line.lower() == "[databases]"
            continue
        if in_databases and re.match(rf"^{re.escape(database)}\s*=", line):
            match = re.search(r"\bhost=(\S+)", line)
            return match.group(1) if match else None
    return None


class PgBouncer:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.pb = cfg.pgbouncer
        self.pods: list[Pod] = []
        self._pending: set[concurrent.futures.Future] = set()

    def kubectl(self, *args: str, timeout: float = 60) -> str:
        return kubectl(self.pb.kube_context, "-n", self.pb.namespace, *args, timeout=timeout)

    def list_pods(self) -> list[Pod]:
        data = json.loads(self.kubectl("get", "pods", "-l", self.pb.selector, "-o", "json"))
        pods = []
        for item in data["items"]:
            if item["metadata"].get("deletionTimestamp"):
                continue
            statuses = item.get("status", {}).get("containerStatuses", [])
            pods.append(Pod(item["metadata"]["name"], bool(statuses) and all(c.get("ready") for c in statuses)))
        return sorted(pods, key=lambda p: p.name)

    def file_host(self, pod: str) -> str | None:
        """The database host in the pod's mounted pgbouncer.ini: the ConfigMap, once kubelet has synced it.
        PgBouncer itself only reads it at RELOAD."""
        ini = self.kubectl("exec", pod, "-c", self.pb.container, "--", "cat", self.pb.config_path, timeout=30)
        return parse_database_host(ini, self.cfg.database)

    def _connect(self, pod: Pod) -> None:
        proc = subprocess.Popen(
            [
                "kubectl",
                "--context",
                self.pb.kube_context,
                "-n",
                self.pb.namespace,
                "port-forward",
                f"pod/{pod.name}",
                f"0:{self.pb.port}",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        pod.port_forward = proc
        assert proc.stdout is not None
        stdout = proc.stdout
        ports: queue.Queue[int | None] = queue.Queue(maxsize=1)

        def drain() -> None:
            announced = False
            for line in stdout:
                match = re.search(r"127\.0\.0\.1:(\d+)", line)
                if match and not announced:
                    ports.put(int(match.group(1)))
                    announced = True
            if not announced:
                ports.put(None)

        threading.Thread(target=drain, daemon=True).start()
        try:
            pod.local_port = ports.get(timeout=30)
        except queue.Empty:
            raise RuntimeError(f"port-forward to {pod.name} didn't start within 30 seconds") from None
        if pod.local_port is None:
            raise RuntimeError(f"port-forward to {pod.name} exited before listening (output withheld)")

        def admin_conn() -> psycopg.Connection:
            return psycopg.connect(
                host="127.0.0.1",
                port=pod.local_port,
                user=self.pb.admin_user,
                password=self.cfg.secret("pgbouncer_admin_password"),
                dbname="pgbouncer",
                sslmode="prefer",
                connect_timeout=10,
                autocommit=True,
                cursor_factory=psycopg.ClientCursor,  # the admin console only speaks the simple query protocol
                row_factory=dict_row,
            )

        pod.conn = admin_conn()
        pod.spare = admin_conn()

    def __enter__(self) -> PgBouncer:
        self.cfg.secret("pgbouncer_admin_password")  # resolve once before starting concurrent connections
        self.pods = self.list_pods()
        if not self.pods:
            raise RuntimeError(f"no pods match {self.pb.selector} in {self.pb.namespace}")
        errors = {k: v for k, v in self.on_all(self._connect, timeout=60).items() if isinstance(v, Exception)}
        if errors:
            self.__exit__()
            raise RuntimeError(f"couldn't reach PgBouncer's admin console: {errors}")
        return self

    def __exit__(self, *exc: Any) -> None:
        for pod in self.pods:
            # Break a stuck admin query before closing its connection (close waits on psycopg's connection lock).
            if pod.port_forward:
                pod.port_forward.terminate()
                try:
                    pod.port_forward.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    pod.port_forward.kill()
                    pod.port_forward.wait(timeout=5)
            for conn in (pod.conn, pod.spare):
                if conn is not None:
                    with contextlib.suppress(Exception):
                        conn.close()

    def on_all(self, fn: Callable[[Pod], Any], timeout: float | None = None) -> dict[str, Any]:
        """Runs fn on every pod at once; returns {pod: result or the exception it raised}."""
        results: dict[str, Any] = {}
        # Not a `with` block: leaving one waits for every thread, and a stuck PAUSE must not hold up an abort.
        pool = concurrent.futures.ThreadPoolExecutor(len(self.pods))
        futures = {pool.submit(fn, pod): pod.name for pod in self.pods}
        try:
            done, pending = concurrent.futures.wait(futures, timeout=timeout)
            for f in done:
                try:
                    results[futures[f]] = f.result()
                except Exception as e:
                    results[futures[f]] = RuntimeError(error_detail(e)) if isinstance(e, psycopg.Error) else e
            for f in pending:
                results[futures[f]] = TimeoutError("timed out")
        except BaseException:
            for future in futures:
                future.cancel()  # do not start an unsent PAUSE after its abort handler has resumed traffic
            raise
        finally:
            self._pending.update(f for f in futures if not f.done())
            self._pending = {f for f in self._pending if not f.done()}
            pool.shutdown(wait=False, cancel_futures=True)
        return results

    def running_host(self, pod: Pod) -> str | None:
        """The database host PgBouncer is using now (SHOW DATABASES)."""
        for row in pod.admin("SHOW DATABASES"):
            if row["name"] == self.cfg.database:
                return row["host"]
        return None

    def resume_all(self) -> dict[str, Any]:
        def resume(pod: Pod) -> str:
            assert pod.spare is not None
            try:
                pod.spare.execute("RESUME")
            except psycopg.Error as e:
                if "not paused" not in str(e).lower():
                    raise
            return "resumed"

        pending = set(self._pending)
        results = self.on_all(resume, timeout=15)
        if pending:
            # A timed-out/interrupted PAUSE might not have reached the server before the first RESUME. Wait for
            # those requests to settle, then confirm RESUME once more, so a late PAUSE cannot silently win.
            _, pending = concurrent.futures.wait(pending, timeout=2)
            if pending:
                results = self.on_all(resume, timeout=15)
                _, pending = concurrent.futures.wait(pending, timeout=2)
            if pending:
                results["in-flight commands"] = TimeoutError("prior admin commands have not settled; state unknown")
            else:
                results = self.on_all(resume, timeout=15)
        return results
