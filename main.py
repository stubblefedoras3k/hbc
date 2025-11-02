from __future__ import annotations
import os, logging, time, signal, sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

from hibachi_client import HibachiRest
from hibachi_mm_engine import HibachiMarketMakerEngine
from env_config import load_env_config, validate_config

shutdown_requested = False


def signal_handler(signum, frame):
    global shutdown_requested
    print("\n\nShutdown requested...")
    shutdown_requested = True


def setup_logging(log_dir: str, level: str = "INFO"):
    Path(log_dir).mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter(
        "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"
    )
    root = logging.getLogger()
    root.setLevel(getattr(logging, level.upper(), logging.INFO))

    fh = RotatingFileHandler(
        os.path.join(log_dir, "app.log"),
        maxBytes=5_000_000, backupCount=5, encoding="utf-8"
    )
    fh.setFormatter(fmt)
    root.addHandler(fh)

    ch = logging.StreamHandler()
    ch.setFormatter(fmt)
    root.addHandler(ch)


def step_with_retry(mm: HibachiMarketMakerEngine, max_retries: int = 3) -> bool:
    import requests

    for attempt in range(max_retries):
        try:
            mm.step()
            return True
        except requests.exceptions.ConnectionError as e:
            logging.warning("Connection error (attempt %d/%d): %s",
                            attempt + 1, max_retries, e)
            if attempt < max_retries - 1:
                time.sleep(2 ** attempt)
            continue
        except Exception as e:
            logging.error("Step error: %s", e, exc_info=True)
            return False

    logging.error("Step failed after %d retries", max_retries)
    return False


def main():
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    if not os.path.exists('.env'):
        print("ERROR: .env file not found!")
        print("Run: cp .env.example .env")
        print("Then edit .env with your API keys")
        sys.exit(1)

    print("Loading configuration...")
    cfg = load_env_config()

    try:
        validate_config(cfg)
    except AssertionError as e:
        print(f"ERROR: Invalid configuration: {e}")
        sys.exit(1)

    setup_logging(cfg["logging"]["dir"], cfg["logging"]["level"])
    log = logging.getLogger("main")

    log.info("=" * 70)
    log.info("Hibachi Market Maker Bot")
    log.info("=" * 70)
    log.info("Symbol: %s", cfg['bot']['symbol'])
    log.info("Base order: %.1f%% | Inv budget: %.1f%%",
             cfg['bot']['baseOrderPct'], cfg['bot']['invBudgetPct'])

    api = cfg["api"]
    bot = cfg["bot"]

    log.info("Initializing Hibachi client...")
    rest = HibachiRest(
        api_url=api["apiUrl"],
        data_api_url=api["dataApiUrl"],
        api_key=api["apiKey"],
        account_id=api["accountId"],
        private_key=api["privateKey"]
    )

    mm = HibachiMarketMakerEngine(rest, bot, cfg["logging"]["dir"])

    try:
        log.info("Bootstrapping contract specifications...")
        mm.bootstrap_markets()

        log.info("Bootstrapping ATR from historical data...")
        mm.bootstrap_atr()

        log.info("Bootstrapping account equity and position...")
        mm.bootstrap_equity_and_pos()
    except Exception as e:
        log.error("Bootstrap failed: %s", e, exc_info=True)
        sys.exit(1)

    log.info("=" * 70)
    log.info("Bot is RUNNING | Press Ctrl+C to stop")
    log.info("=" * 70)

    loop_interval = 5.0
    last_step = 0

    try:
        while not shutdown_requested:
            now = time.time()
            if now - last_step >= loop_interval:
                if step_with_retry(mm):
                    last_step = now
            time.sleep(0.5)
    except KeyboardInterrupt:
        log.info("Keyboard interrupt")
    finally:
        log.info("Shutting down...")
        try:
            mm._cancel_both()
            time.sleep(1)
            mm._force_equity_update()
            log.info("Final equity: $%.2f | Position: %.4f",
                     mm.state.equity_usd, mm.state.pos_qty)
        except Exception as e:
            log.error("Shutdown error: %s", e)
        log.info("Goodbye!")


if __name__ == "__main__":
    main()