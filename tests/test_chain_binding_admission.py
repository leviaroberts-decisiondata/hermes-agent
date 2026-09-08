"""Deploy-friction batch #3 — admission-time chain-binding check.

`dd-chain-driver --check-binding` answers whether a WTS task a caller is about to
submit (e.g. a deploy_submit) CONTRADICTS the WTS task the live chain on that
route_key is already bound to. This closes the residual gap that the bind path
(`cmd_set_wts`'s `wts-contention` refusal) covered but the deploy-submit path did
not: a submit inheriting/overriding a different `wts_task_id` would silently link
the deploy row to the wrong tracker task, caught only later at parity-audit.

The verdict is fail-open by construction — only a live chain with a NON-EMPTY
binding that no matched chain agrees with yields `contradiction`; everything else
(no route_key, no live chain, unbound `no-task:` chain, error) is `no-binding` so
an over-eager reject can never block a legitimate build.
"""

import importlib.machinery
import importlib.util
import io
import json
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace

import pytest

_DRIVER = Path(__file__).resolve().parent.parent.parent / "bin" / "dd-chain-driver"


@pytest.fixture(scope="module")
def driver():
    if not _DRIVER.exists():
        pytest.skip(f"chain driver not present at {_DRIVER}")
    loader = importlib.machinery.SourceFileLoader("ddchain_under_test", str(_DRIVER))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


def _run(driver, ledger, wts, route_key):
    driver.load_ledger = lambda: ledger  # inject synthetic ledger; never touches disk
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = driver.cmd_check_binding(SimpleNamespace(wts=wts, route_key=route_key))
    assert rc == 0  # always soft (JSON verdict), like set-wts / set-deploy-row
    return json.loads(buf.getvalue().strip().splitlines()[-1])


RK = "slack:cX:uY:1780000000.000001"


def _chain(route_key=RK, status="active", wts="A", chain_id="c1"):
    return {"chain_id": chain_id, "route_key": route_key, "status": status, "wts_task": wts}


def test_matching_task_is_ok(driver):
    out = _run(driver, [_chain(wts="A")], "A", RK)
    assert out["verdict"] == "ok"
    assert out["bound_wts"] == "A"


def test_contradicting_task_is_flagged(driver):
    out = _run(driver, [_chain(wts="A")], "B", RK)
    assert out["verdict"] == "contradiction"
    assert out["bound_wts"] == "A"
    assert out["requested_wts"] == "B"


def test_unbound_no_task_chain_fails_open(driver):
    # A fresh slack chain mints with wts_task None (no-task anchor) before its
    # deliverable task is bound — nothing to contradict yet.
    out = _run(driver, [_chain(wts=None)], "B", RK)
    assert out["verdict"] == "no-binding"


def test_no_live_chain_fails_open(driver):
    assert _run(driver, [], "B", RK)["verdict"] == "no-binding"


def test_bound_but_not_live_fails_open(driver):
    # A done/aborted chain is not an admission gate — only active/blocked count.
    out = _run(driver, [_chain(status="done", wts="A")], "B", RK)
    assert out["verdict"] == "no-binding"


def test_missing_args_fail_open(driver):
    assert _run(driver, [_chain(wts="A")], "B", "")["verdict"] == "no-binding"
    assert _run(driver, [_chain(wts="A")], "", RK)["verdict"] == "no-binding"


def test_multiple_live_chains_request_agrees_with_one(driver):
    led = [_chain(wts="A", chain_id="c1"),
           _chain(status="blocked", wts="B", chain_id="c2")]
    assert _run(driver, led, "A", RK)["verdict"] == "ok"


def test_multiple_live_chains_request_agrees_with_none(driver):
    led = [_chain(wts="A", chain_id="c1"),
           _chain(status="blocked", wts="B", chain_id="c2")]
    out = _run(driver, led, "C", RK)
    assert out["verdict"] == "contradiction"
    assert set(out["all_bound"]) == {"A", "B"}


def test_other_route_key_is_ignored(driver):
    # A binding on a DIFFERENT route_key must not contradict this request.
    led = [_chain(route_key="slack:other:u:1.1", wts="A")]
    assert _run(driver, led, "B", RK)["verdict"] == "no-binding"
