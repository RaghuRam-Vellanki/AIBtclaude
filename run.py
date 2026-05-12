"""
run.py — single entry-point for cloning this repo onto another machine and
starting the bot. The dashboard is the orchestrator: it spins up the three
asset agents (BTC live demo, XAU paper-sim pod, NIFTY paper-sim pod) and
serves the UI at http://localhost:8080.

Examples:
    python run.py                    # launch dashboard + all agents (default)
    python run.py --agent dashboard  # dashboard only
    python run.py --agent xau        # XAU agent alone (no web UI)
    python run.py --agent btc        # BTC agent alone
    python run.py --agent nifty      # NIFTY agent alone
    python run.py --check            # environment self-check, no run
    python run.py --port 9090        # serve dashboard on a different port

The dashboard reads .env on startup. Copy .env.example → .env first and fill
in the keys you want (all optional except for the asset you intend to trade
live — BTC live mode needs Alpaca keys; XAU/NIFTY paper-sim need no keys).
"""
from __future__ import annotations

import argparse
import os
import sys
from importlib import import_module
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))


REQUIRED_PACKAGES = [
    # (import name, install hint shown if missing)
    ("pandas", "pandas>=2.0.0"),
    ("numpy", "numpy>=1.26.0"),
    ("requests", "requests"),
    ("yfinance", "yfinance>=0.2.40"),
    ("dotenv", "python-dotenv>=1.0.0"),
    ("fastapi", "fastapi>=0.111.0"),
    ("uvicorn", "uvicorn>=0.29.0"),
]


def _self_check() -> bool:
    """Verify Python version, dependencies, and .env presence."""
    ok = True
    print("== Environment self-check ==")
    py = sys.version_info
    if py < (3, 10):
        print(f"  [!] Python {py.major}.{py.minor} detected — 3.10+ recommended")
        ok = False
    else:
        print(f"  [+] Python {py.major}.{py.minor}.{py.micro}")

    missing = []
    for name, hint in REQUIRED_PACKAGES:
        try:
            import_module(name)
        except Exception:
            missing.append(hint)
    if missing:
        print("  [!] Missing packages:")
        for m in missing:
            print(f"        - {m}")
        print("      Install: pip install -r requirements.txt")
        ok = False
    else:
        print("  [+] All required packages importable")

    env_path = ROOT / ".env"
    if not env_path.exists():
        print("  [!] No .env found. Copy .env.example → .env to configure keys.")
        print("      (paper-sim XAU/NIFTY pods run without keys; BTC live needs Alpaca.)")
    else:
        print("  [+] .env present")

    data_dir = ROOT / "data"
    if not (data_dir / "event_calendar.json").exists():
        print("  [!] data/event_calendar.json missing — event blackout will no-op")
        ok = False
    else:
        print("  [+] event_calendar.json present")

    print("== Check complete ==")
    return ok


def _start_dashboard(port: int) -> None:
    """Dashboard imports and starts all 3 agents internally; serves UI at /."""
    print("=" * 64)
    print(f"  Multi-Asset Trading Dashboard  ->  http://localhost:{port}")
    print("  Assets: BTC (Alpaca live, demo-safe) | XAU (paper-sim pod)")
    print("          NIFTY 50 (paper-sim pod)")
    print("  Strategies: 9 XAU | 7 BTC | 12 NIFTY (cluster-aware)")
    print("=" * 64)
    import uvicorn
    import dashboard  # noqa: F401  — registers FastAPI app
    uvicorn.run(dashboard.app, host="0.0.0.0", port=port, reload=False)


def _start_agent(name: str) -> None:
    """Run one asset agent in the foreground, no UI."""
    name = name.lower()
    if name == "btc":
        from agent import BTCTradingAgent
        BTCTradingAgent().run()
    elif name == "xau":
        from agent_xau import XAUTradingAgent
        XAUTradingAgent().run()
    elif name == "nifty":
        from agent_nifty import NIFTYTradingAgent
        NIFTYTradingAgent().run()
    else:
        raise SystemExit(f"unknown agent '{name}' (expected btc|xau|nifty)")


def main() -> None:
    p = argparse.ArgumentParser(
        description="Launch the multi-asset trading bot (dashboard or single agent).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--agent", "-a",
                   choices=["dashboard", "all", "btc", "xau", "nifty"],
                   default="dashboard",
                   help="What to start (default: dashboard, which boots all 3 agents)")
    p.add_argument("--port", type=int, default=8080,
                   help="Dashboard port (default 8080)")
    p.add_argument("--check", action="store_true",
                   help="Run the environment self-check and exit")
    args = p.parse_args()

    if args.check:
        sys.exit(0 if _self_check() else 1)

    # Always warn if obvious issues, but don't block startup.
    _self_check()

    if args.agent in ("dashboard", "all"):
        _start_dashboard(args.port)
    else:
        _start_agent(args.agent)


if __name__ == "__main__":
    main()
