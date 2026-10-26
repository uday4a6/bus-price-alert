#!/usr/bin/env python3
"""AbhiBus price-drop watcher.

Polls AbhiBus's bus-list API for a route/date, remembers each bus's lowest
seen price, and alerts (macOS notification + optional ntfy.sh phone push)
when a price drops.

Examples:
    # one check (good for cron)
    python3 bus_price_alert.py --date 20-10-2026

    # keep running, check every 15 min, only AC sleepers under 1500
    python3 bus_price_alert.py --date 20-10-2026 --bus-type "AC Sleeper" --max-price 1500 --every 15
"""

import argparse
import json
import subprocess
import sys
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

API_URL = "https://www.abhibus.com/buslist/v3/services"
HEADERS = {
    "Content-Type": "application/json",
    "X-APP-NAME": "nextgenweb",
    "Origin": "https://www.abhibus.com",
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/129.0 Safari/537.36",
}


def fetch_services(src, src_id, dst, dst_id, date):
    """date: DD-MM-YYYY (same as AbhiBus URL)."""
    jdate = datetime.strptime(date, "%d-%m-%Y").strftime("%Y-%m-%d")
    payload = {
        "source": src,
        "sourceid": src_id,
        "destination": dst,
        "destinationid": dst_id,
        "jdate": jdate,
        "prd": "mobile",
        "filters": "1",
        "isReturnJourney": "0",
        "version": "114",
    }
    req = urllib.request.Request(
        API_URL, data=json.dumps(payload).encode(), headers=HEADERS, method="POST"
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        data = json.load(resp)
    if data.get("status") != "success":
        raise RuntimeError(f"API error: {data.get('message')}")
    return data.get("services", [])


def summarize(svc):
    fares = svc.get("fares", {})
    return {
        "key": str(svc.get("serviceId")),  # serviceName/serviceKey are not unique
        "operator": svc.get("travelerAgentName", "?"),
        "bus_type": svc.get("busTypeName", ""),
        "departs": svc.get("timings", {}).get("startTime", ""),
        "price": fares.get("offerPrice") or fares.get("fare"),
        "site_price": fares.get("fare"),  # shown on the website, before the auto discount
        "seats": svc.get("seatStats", {}).get("availableSeats"),
        "starts_at": svc.get("timings", {}).get("startTimeDateFormat"),
    }


def show_price(bus):
    site = bus.get("site_price")
    if site and site != bus["price"]:
        return f"Rs {bus['price']} (site Rs {site})"
    return f"Rs {bus['price']}"


def matches(bus, args):
    if bus["price"] is None or not bus["seats"]:
        return False
    if args.min_lead and bus["starts_at"]:
        mins_left = (datetime.fromisoformat(bus["starts_at"]) - datetime.now().astimezone()).total_seconds() / 60
        if mins_left < args.min_lead:
            return False
    if (args.depart_after or args.depart_before) and bus["starts_at"]:
        hhmm = datetime.fromisoformat(bus["starts_at"]).strftime("%H:%M")
        if args.depart_after and hhmm < args.depart_after:
            return False
        if args.depart_before and hhmm > args.depart_before:
            return False
    if args.operator and args.operator.lower() not in bus["operator"].lower():
        return False
    if args.bus_type:
        # "NON-AC" contains "ac"; treat it as its own word so "AC" doesn't match it
        words = set(bus["bus_type"].lower().replace("non-ac", "nonac").replace("(", " ").split())
        wanted = args.bus_type.lower().replace("non-ac", "nonac").split()
        # prefix match, so "9600" also matches "9600S"
        if not all(any(x.startswith(w) for x in words) for w in wanted):
            return False
    return True


def notify(title, message, ntfy_topic=None):
    print(f"\n*** {title}\n{message}\n")
    if sys.platform == "darwin":
        script = f"display notification {json.dumps(message)} with title {json.dumps(title)} sound name \"Glass\""
        subprocess.run(["osascript", "-e", script], check=False)
    if ntfy_topic:
        req = urllib.request.Request(
            f"https://ntfy.sh/{ntfy_topic}",
            data=message.encode(),
            headers={"Title": title},
            method="POST",
        )
        try:
            urllib.request.urlopen(req, timeout=15)
        except Exception as e:
            print(f"ntfy failed: {e}")


def check_once(args, state_file):
    state = json.loads(state_file.read_text()) if state_file.exists() else {}
    buses = [summarize(s) for s in fetch_services(args.src, args.src_id, args.dst, args.dst_id, args.date)]
    buses = [b for b in buses if matches(b, args)]
    now = datetime.now().strftime("%H:%M")

    if not buses:
        print(f"[{now}] no matching buses with seats")
        return

    drops = []
    for b in buses:
        prev = state.get(b["key"], {}).get("low")
        if prev is not None and b["price"] < prev - args.min_drop + 1e-9:
            drops.append((b, prev))
        low = b["price"] if prev is None else min(prev, b["price"])
        state[b["key"]] = {"low": low, "last": b["price"], "operator": b["operator"]}

    cheapest = min(buses, key=lambda b: b["price"])
    print(
        f"[{now}] {len(buses)} buses | cheapest Rs {cheapest['price']} "
        f"{cheapest['operator']} ({cheapest['departs']}, {cheapest['bus_type']})"
    )

    if args.test_alert:
        top = sorted(buses, key=lambda b: b["price"])[:5]
        notify(
            f"TEST alert {args.src}->{args.dst} {args.date}",
            "\n".join(f"{show_price(b)}  {b['operator']} {b['departs']} ({b['seats']} seats)" for b in top),
            args.ntfy,
        )

    lines = [
        f"Rs {prev} -> {show_price(b)}  {b['operator']} {b['departs']} ({b['bus_type']}, {b['seats']} seats)"
        for b, prev in sorted(drops, key=lambda x: x[0]["price"])
        if args.max_price is None or b["price"] <= args.max_price
    ]
    if lines:
        notify(f"Bus price drop {args.src}->{args.dst} {args.date}", "\n".join(lines[:5]), args.ntfy)
    below = args.alert_below if args.alert_below is not None else args.max_price
    if below is not None and cheapest["price"] <= below and not state.get("_under_max_alerted"):
        notify(
            f"Bus under Rs {below}: {args.src}->{args.dst}",
            f"{show_price(cheapest)} {cheapest['operator']} {cheapest['departs']} ({cheapest['bus_type']})",
            args.ntfy,
        )
        state["_under_max_alerted"] = True

    state_file.write_text(json.dumps(state, indent=1))


def main():
    p = argparse.ArgumentParser(description="Alert when AbhiBus prices drop")
    p.add_argument("--src", default="Bengaluru")
    p.add_argument("--src-id", type=int, default=7)
    p.add_argument("--dst", default="Vijayawada")
    p.add_argument("--dst-id", type=int, default=5)
    p.add_argument("--date", required=True, help="DD-MM-YYYY, as in AbhiBus URL, or 'today' (IST)")
    p.add_argument("--operator", help="substring of operator name, e.g. 'IntrCity'")
    p.add_argument("--bus-type", help="words that must appear in bus type, e.g. 'AC Sleeper'")
    p.add_argument("--max-price", type=float, help="only alert at or below this price")
    p.add_argument("--alert-below", type=float, help="extra alert when cheapest bus is at or below this price")
    p.add_argument("--min-drop", type=float, default=1, help="ignore drops smaller than this (Rs)")
    p.add_argument("--every", type=float, help="repeat every N minutes (omit for single check)")
    p.add_argument("--for", dest="duration", type=float, help="stop after N minutes")
    p.add_argument("--min-lead", type=float, help="ignore buses departing within N minutes")
    p.add_argument("--depart-after", help="only buses departing at/after HH:MM (24h), e.g. 17:00")
    p.add_argument("--depart-before", help="only buses departing at/before HH:MM (24h), e.g. 22:00")
    p.add_argument("--ntfy", help="ntfy.sh topic for phone push (install ntfy app, subscribe to topic)")
    p.add_argument("--test-alert", action="store_true", help="always send cheapest buses (to verify alerts)")
    p.add_argument("--state-dir", default=str(Path(__file__).parent / ".bus_alert_state"))
    args = p.parse_args()
    if args.date.lower() == "today":
        args.date = datetime.now(timezone(timedelta(hours=5, minutes=30))).strftime("%d-%m-%Y")

    state_dir = Path(args.state_dir)
    state_dir.mkdir(parents=True, exist_ok=True)
    state_file = state_dir / f"{args.src_id}_{args.dst_id}_{args.date}.json"

    deadline = time.time() + args.duration * 60 if args.duration else None
    while True:
        try:
            check_once(args, state_file)
        except Exception as e:
            print(f"[{datetime.now():%H:%M}] check failed: {e}")
        if not args.every:
            break
        if deadline and time.time() + args.every * 60 > deadline:
            print(f"[{datetime.now():%H:%M}] done")
            break
        time.sleep(max(args.every, 1) * 60)


if __name__ == "__main__":
    main()
