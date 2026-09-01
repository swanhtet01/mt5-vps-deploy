"""Make the VPS scripts importable off Windows so they can be tested at all.

killswitch_monitor.py imports MetaTrader5 at module scope and pulls helpers from the
mt5_agent package that ships under hotfix/src. Neither is available on a Linux CI box, so
this stubs the MetaTrader5 module and puts hotfix/src on the path. The stub deliberately
carries only the constants and functions the tests touch: anything else raises, so a test
that reaches the broker by accident fails loudly instead of silently passing.
"""

from __future__ import annotations

import os
import sys
import tempfile
import types
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
# The deployed strategy scripts live here and import each other by bare name.
sys.path.insert(0, str(REPO_ROOT / "hotfix" / "scripts"))

# paths.py resolves its roots from the environment at import and defaults to C:\mt5-paper,
# which on Linux is a *relative* path — anything that writes would create a directory
# literally named "C:\mt5-paper" inside the repo. Point both roots at a temp dir so a stray
# write lands somewhere harmless rather than in the working tree.
_SANDBOX = Path(tempfile.mkdtemp(prefix="mt5-vps-deploy-tests-"))
os.environ.setdefault("MT5_PAPER", str(_SANDBOX / "paper"))
os.environ.setdefault("MT5_REPO", str(_SANDBOX / "repo"))


def _pin_mt5_agent_to_this_repo() -> None:
    """Force ``mt5_agent`` to resolve to THIS repo's copy under hotfix/src.

    hotfix/src/mt5_agent has no __init__.py, so it is only a namespace package portion —
    any real mt5_agent package on the path (an editable install of the monorepo's copy,
    say) wins the import and silently supplies a DIFFERENT, older module set that has no
    mt5_execution at all. Tests would then either fail confusingly or, worse, pass against
    code the VPS does not run. Binding __path__ explicitly makes the resolution
    unambiguous regardless of what else is installed.
    """
    for name in [n for n in sys.modules if n == "mt5_agent" or n.startswith("mt5_agent.")]:
        del sys.modules[name]
    package = types.ModuleType("mt5_agent")
    package.__path__ = [str(REPO_ROOT / "hotfix" / "src" / "mt5_agent")]
    sys.modules["mt5_agent"] = package


_pin_mt5_agent_to_this_repo()


def _unavailable(name: str):
    def _raise(*args, **kwargs):
        raise AssertionError(
            f"test reached the real MetaTrader5 API via {name}() — stub it in the test instead"
        )

    return _raise


def _install_metatrader5_stub() -> None:
    if "MetaTrader5" in sys.modules:
        return
    stub = types.ModuleType("MetaTrader5")
    # Retcodes the scripts compare against.
    stub.TRADE_RETCODE_DONE = 10009
    stub.TRADE_RETCODE_DONE_PARTIAL = 10010
    stub.TRADE_ACTION_DEAL = 1
    stub.ORDER_TYPE_BUY = 0
    stub.ORDER_TYPE_SELL = 1
    stub.ORDER_TIME_GTC = 0
    stub.ORDER_FILLING_IOC = 1
    stub.ORDER_FILLING_FOK = 0
    stub.ORDER_FILLING_RETURN = 2
    stub.POSITION_TYPE_BUY = 0
    for call in (
        "initialize", "shutdown", "account_info", "history_deals_get", "positions_get",
        "symbol_info", "symbol_info_tick", "copy_rates_from_pos", "order_send",
        "order_check", "last_error",
    ):
        setattr(stub, call, _unavailable(call))
    sys.modules["MetaTrader5"] = stub


_install_metatrader5_stub()
