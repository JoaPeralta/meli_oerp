"""Run only in the no-egress, disposable Odoo/PostgreSQL test harness."""
from concurrent.futures import ThreadPoolExecutor
import re
from threading import Barrier, current_thread
from unittest.mock import patch

from odoo import SUPERUSER_ID, api
from odoo.modules.registry import Registry
from odoo.tools import config


def run_disposable_race(dbname):
    """Force two real PostgreSQL transactions past the same absent-row read."""
    if (not re.fullmatch(r"metadata_[a-f0-9]{12}", dbname)
            or config["db_host"] != "127.0.0.1" or config["db_user"] != "metadata_lab"
            or not 49152 <= int(config["db_port"]) <= 65535):
        raise RuntimeError("The fiscal concurrency probe requires an isolated lab target.")
    registry = Registry(dbname)
    with registry.cursor() as cr:
        env = api.Environment(cr, SUPERUSER_ID, {})
        company = env.ref("base.main_company")
        journal = env["account.journal"].with_company(company).create({
            "name": "Synthetic Fiscal Race", "code": "FRC", "type": "sale", "company_id": company.id,
        })
        invoice = env["account.move"].with_company(company).create({
            "company_id": company.id, "journal_id": journal.id, "move_type": "out_invoice",
        })
        scope = dict(company_id=company.id, move_id=invoice.id, seller_id="17",
                     target_id="9000071", pack_id="9000071", order_ids=["9000023"])
        model = type(env["mercadolibre.fiscal.upload"])

    barrier, server_processes = Barrier(2), []
    search = model.search

    def gated_search(self, domain, *args, **kwargs):
        rows = search(self, domain, *args, **kwargs)
        if current_thread().name.startswith("fiscal-race"):
            if rows:
                raise AssertionError("The reservation must not exist before either insert.")
            self.env.cr.execute("SELECT pg_backend_pid()")
            server_processes.append(self.env.cr.fetchone()[0])
            barrier.wait(timeout=15)
        return rows

    def reserve():
        with registry.cursor() as cr:
            env = api.Environment(cr, SUPERUSER_ID, {})
            return env["mercadolibre.fiscal.upload"]._operation(scope, "reserve")

    with (
        patch.object(model, "search", gated_search),
        ThreadPoolExecutor(2, thread_name_prefix="fiscal-race") as pool,
    ):
        results = [pool.submit(reserve) for _ in range(2)]
        results = [future.result(timeout=25) for future in results]
    assert len(set(server_processes)) == 2, server_processes
    assert sum(result is not None for result in results) == 1, results
    with registry.cursor() as cr:
        env = api.Environment(cr, SUPERUSER_ID, {})
        model = env["mercadolibre.fiscal.upload"]
        assert model.search_count([("seller_id", "=", "17"), ("target_id", "=", "9000071")]) == 1
        assert model._operation(scope, "reserve") is None
    print("FISCAL_RACE_OK two distinct PostgreSQL processes; one durable send reservation", flush=True)
