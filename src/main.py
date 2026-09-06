"""Entry point. Fetch -> dedupe against history -> prefilter -> score -> deliver."""

import os
import json
import logging
import argparse
from pathlib import Path
from datetime import datetime, timezone, timedelta

import yaml

import sources
import score as scoring
import sinks

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
log = logging.getLogger("jobradar")

ROOT = Path(__file__).resolve().parent.parent
SEEN_PATH = ROOT / "data" / "seen.json"


def load_seen(retain_days=60):
    """Job ids we've already reported, with a TTL so the file can't grow forever."""
    if not SEEN_PATH.exists():
        return {}
    seen = json.loads(SEEN_PATH.read_text())
    cutoff = (datetime.now(timezone.utc) - timedelta(days=retain_days)).isoformat()
    return {k: v for k, v in seen.items() if v >= cutoff}


def save_seen(seen, new_ids):
    now = datetime.now(timezone.utc).isoformat()
    seen.update({i: now for i in new_ids})
    SEEN_PATH.parent.mkdir(parents=True, exist_ok=True)
    SEEN_PATH.write_text(json.dumps(seen, indent=0, sort_keys=True))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true",
                    help="print results instead of writing to sheet/email")
    args = ap.parse_args()

    cfg = yaml.safe_load((ROOT / "config.yaml").read_text())
    profile = json.loads((ROOT / "profile.json").read_text())
    resume = (ROOT / "resume.txt").read_text()

    jobs = sources.fetch_all(cfg["queries"], cfg["locations"], cfg["sources"])
    log.info("Fetched %d unique jobs", len(jobs))

    seen = load_seen(cfg.get("retain_days", 60))
    fresh = [j for j in jobs if j["id"] not in seen]
    log.info("%d are new since last run", len(fresh))
    if not fresh:
        log.info("Nothing new. Done.")
        return

    candidates = scoring.prefilter(fresh, profile, keep=cfg.get("llm_batch_cap", 40))
    scored = scoring.llm_score(candidates, resume, profile)

    threshold = cfg.get("min_score", 6)
    keepers = [j for j in scored if j["score"] >= threshold]
    log.info("%d cleared the score threshold of %d", len(keepers), threshold)

    if args.dry_run:
        for j in keepers:
            print(f"[{j['score']}] {j['title']} @ {j['company']} ({j['location']})")
            print(f"     {j.get('reason','')}")
            print(f"     {j['url']}\n")
        return

    delivered = False
    if keepers:
        # Each sink is independent: a failing sheet shouldn't cost you the
        # email, and vice versa.
        for name, fn in (
            ("sheet", lambda: sinks.push_to_sheet(keepers)),
            ("email", lambda: sinks.send_email(keepers, top_n=cfg.get("email_top_n", 15))),
        ):
            try:
                fn()
                delivered = True
            except Exception as e:
                log.error("Delivery to %s failed: %s", name, e)

    # Only forget these jobs once they actually reached you. If every sink
    # failed, leave them unseen so tomorrow's run tries again.
    if delivered or not keepers:
        save_seen(seen, [j["id"] for j in fresh])
    else:
        log.warning("Nothing delivered -- not marking jobs as seen")


if __name__ == "__main__":
    main()
