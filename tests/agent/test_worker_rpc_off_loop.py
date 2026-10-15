"""A worker's own event loop must not cost it persistence.

``WorkerRPC`` used to raise ``synchronous_rpc_on_event_loop`` whenever the calling thread owned a
running loop. That is correct for the owner's loop — blocking it would deadlock the very socket the
call needs — but a worker subprocess's loop belongs to an auxiliary call (title, memory, background
review), and the RPC target is another process entirely. Those calls now run on a helper thread;
ownership is proven offline from ``gateway.pid`` so only the owner keeps the hard failure.
"""
from contextlib import contextmanager
import asyncio
import json
import os
import threading
from types import SimpleNamespace

import pytest

from agent.runtime_session_store import WorkerPersistenceError, WorkerRPC


def _install_fake_owner(tmp_path, monkeypatch, *, owner_pid, error=None):
    """Point ``WorkerRPC`` at a fake ready owner publishing ``owner_pid`` as its identity.

    ``error`` (when given) becomes the owner's JSON-RPC error message. Returns the recorded call
    kwargs and the name of the thread each connect ran on, so a test can prove where the work ran.
    """
    endpoint = SimpleNamespace(api_origin='http://127.0.0.1:1234')
    monkeypatch.setattr('hermes_cli.gateway_runtime.discover_gateway_endpoint',
                        lambda *a, **k: SimpleNamespace(state='ready', endpoint=endpoint))
    monkeypatch.setattr('hermes_cli.gateway_runtime.control_home_for', lambda home, endpoint: home)
    monkeypatch.setattr('hermes_cli.gateway_runtime_discovery.query_identify', lambda *a, **k: {'pid': -1})
    monkeypatch.setattr('hermes_cli.gateway_client._session_ticket', lambda *a, **k: 'ticket')
    monkeypatch.setattr('gateway.status.live_gateway_pid_for_home', lambda home: owner_pid)
    seen = {'calls': [], 'threads': []}

    @contextmanager
    def connect(url, **kwargs):
        seen['calls'].append(kwargs)
        seen['threads'].append(threading.current_thread().name)
        payload = {'id': 1, 'error': {'message': error}} if error else {'id': 1, 'result': {'ok': True}}
        yield SimpleNamespace(send=lambda value: None, recv=lambda **k: json.dumps(payload))

    monkeypatch.setattr('websockets.sync.client.connect', connect)
    return seen


def test_off_loop_worker_call_persists_from_a_running_loop(tmp_path, monkeypatch):
    """The reported crash: persistence issued from a worker's own auxiliary loop now succeeds."""
    seen = _install_fake_owner(tmp_path, monkeypatch, owner_pid=1)
    rpc = WorkerRPC(tmp_path)
    observed = {}

    async def persist():
        observed['loop_running'] = asyncio.get_running_loop() is not None
        return rpc('worker.persist')

    assert asyncio.run(persist()) == {'ok': True}
    assert observed['loop_running'] is True
    # The blocking websocket client never touched the loop's thread.
    assert seen['threads'] == ['worker-rpc-off-loop']
    assert all(call['proxy'] is None for call in seen['calls'])


def test_off_loop_owner_loop_keeps_the_hard_failure(tmp_path, monkeypatch):
    """Ownership proof must not weaken the guard: the owner's own loop still deadlocks, so it fails."""
    _install_fake_owner(tmp_path, monkeypatch, owner_pid=os.getpid())
    rpc = WorkerRPC(tmp_path)

    async def persist():
        return rpc('worker.persist')

    with pytest.raises(WorkerPersistenceError, match='synchronous_rpc_on_event_loop'):
        asyncio.run(persist())


def test_off_loop_unprovable_identity_does_not_reblock_the_worker(tmp_path, monkeypatch):
    """An unreadable owner identity fails open: a worker's loop must not lose persistence to it."""
    _install_fake_owner(tmp_path, monkeypatch, owner_pid=1)

    def unreadable(home):
        raise OSError('gateway.pid is not inspectable')

    monkeypatch.setattr('gateway.status.live_gateway_pid_for_home', unreadable)
    rpc = WorkerRPC(tmp_path)

    async def persist():
        return rpc('worker.persist')

    assert asyncio.run(persist()) == {'ok': True}


def test_off_loop_owner_error_reaches_the_caller(tmp_path, monkeypatch):
    """A helper-thread failure surfaces on the caller's thread instead of being swallowed."""
    _install_fake_owner(tmp_path, monkeypatch, owner_pid=1, error='persistence_failed')
    rpc = WorkerRPC(tmp_path)

    async def persist():
        return rpc('worker.persist')

    with pytest.raises(WorkerPersistenceError, match='persistence_failed'):
        asyncio.run(persist())


def test_off_loop_plain_call_still_runs_inline(tmp_path, monkeypatch):
    """Without a loop in the calling thread nothing changes: the call stays on the caller's thread."""
    seen = _install_fake_owner(tmp_path, monkeypatch, owner_pid=1)
    rpc = WorkerRPC(tmp_path)

    assert rpc('worker.persist') == {'ok': True}
    assert seen['threads'] == [threading.current_thread().name]
