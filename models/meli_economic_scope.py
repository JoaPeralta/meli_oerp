"""Private lexical projection authority; never an RPC/context-boolean grant."""
from contextlib import contextmanager
from contextvars import ContextVar

_ACTIVE = ContextVar("meli_economic_projection", default=None)


@contextmanager
def economic_projection_lifetime():
    token = _ACTIVE.set(None)
    try:
        yield
    finally:
        _ACTIVE.reset(token)


def activate_economic_projection(records, remote_id, company_id):
    capability = object()
    _ACTIVE.set((records.env.cr, records.env.uid, company_id, str(remote_id), capability,
                 getattr(records.env, "su", False)))
    return records.with_context(_meli_economic_projection=capability)


def economic_projection_allowed(env, remote_id=None):
    state = _ACTIVE.get()
    return bool(state and state[0] is env.cr and state[1] == env.uid
                and state[2] == env.company.id
                and env.context.get("_meli_economic_projection") is state[4]
                and getattr(env, "su", False) == state[5]
                and (remote_id is None or state[3] == str(remote_id)))


@contextmanager
def economic_snapshot_writer(records):
    with economic_projection_lifetime():
        yield activate_economic_projection(records, records.order_id, records.company_id.id)
