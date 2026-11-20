"""Verbindungstest:  python -m tradebots.check
Prueft Alpaca-Konten, LLM, Reddit und die RSS-Feeds - ohne zu handeln.
"""
from __future__ import annotations

from . import config, llm, sources
from .broker import Broker


def ok(msg):
    print(f"  [OK]   {msg}")


def fail(msg):
    print(f"  [FAIL] {msg}")


def main() -> None:
    print("Alpaca Paper-Konten:")
    seen_accounts: dict[str, str] = {}
    for bot in config.BOT_NAMES:
        cfg = config.bot_config(bot)
        try:
            acct = Broker(cfg).account()
            ok(f"{bot}: Konto {acct.account_number}, Equity ${float(acct.equity):,.2f}")
            if acct.account_number in seen_accounts:
                fail(f"{bot} nutzt DASSELBE Konto wie {seen_accounts[acct.account_number]} -> Ergebnisse vermischen sich!")
            seen_accounts[acct.account_number] = bot
        except Exception as e:  # noqa: BLE001
            fail(f"{bot}: {e}")

    print("LLM:")
    try:
        ok(f"{config.LLM_MODEL} @ {config.LLM_BASE_URL} -> '{llm.ping()}'")
    except Exception as e:  # noqa: BLE001
        fail(str(e))

    print("Datenquellen:")
    for name, fn in sources.SOURCES.items():
        try:
            items = fn()
            latest = items[0]["title"][:70] if items else "-"
            ok(f"{name}: {len(items)} Posts, z.B. '{latest}'")
        except Exception as e:  # noqa: BLE001
            fail(f"{name}: {e}")


if __name__ == "__main__":
    main()
