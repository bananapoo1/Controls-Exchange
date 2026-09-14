from __future__ import annotations
import argparse, logging, os, sys, time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from platform_core import init_db
from billing import init_billing_schema
from intelligence import init_intelligence_schema
from procurement import WEBHOOK_WORKER_INTERVAL_SECONDS, init_procurement_schema, process_due_webhooks

logging.basicConfig(level=getattr(logging, os.getenv("LOG_LEVEL", "INFO").upper(), logging.INFO))
log = logging.getLogger("controls_exchange.webhook_worker")

def run_once():
    init_db(); init_billing_schema(); init_intelligence_schema(); init_procurement_schema()
    stats = process_due_webhooks()
    log.info("webhook_worker claimed=%s delivered=%s failed=%s", stats["claimed"], stats["delivered"], stats["failed"])
    return stats

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--loop", action="store_true")
    args = parser.parse_args()
    if not args.loop:
        run_once()
    else:
        while True:
            try: run_once()
            except Exception: log.exception("webhook_worker_failed")
            time.sleep(WEBHOOK_WORKER_INTERVAL_SECONDS)
