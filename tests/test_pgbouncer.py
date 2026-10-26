import concurrent.futures
import threading
from types import SimpleNamespace

from db_cutover.pgbouncer import PgBouncer, Pod


def test_resume_is_confirmed_after_a_late_pause():
    bouncer = PgBouncer(SimpleNamespace(pgbouncer=None))
    first_resume = threading.Event()
    paused = {"value": False}
    calls = []

    def resume(command):
        calls.append(command)
        paused["value"] = False
        first_resume.set()

    pod = Pod("test", True, spare=SimpleNamespace(execute=resume))
    bouncer.pods = [pod]

    def late_pause():
        assert first_resume.wait(5)
        paused["value"] = True

    with concurrent.futures.ThreadPoolExecutor(1) as pool:
        future = pool.submit(late_pause)
        bouncer._pending.add(future)
        results = bouncer.resume_all()
    assert results == {"test": "resumed"}
    assert calls == ["RESUME", "RESUME"]
    assert paused["value"] is False
