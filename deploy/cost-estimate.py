#!/usr/bin/env python3
"""Hetzner cost estimate for the scheduled voice server.

This script does NOT invent prices: pass the CURRENT rates from the Hetzner
console (and your FX rate) so the estimate is verifiable. Cost check only — it
creates nothing.

    python3 deploy/cost-estimate.py \
        --hourly 0.00XX --hours-week 45 --ipv4-month 0.00XX \
        --vat-pct 19 --eur-usd 1.08 --budget-usd 3.00
"""
from __future__ import annotations

import argparse


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--hourly", type=float, required=True, help="server price EUR/hour (verify)")
    p.add_argument("--hours-week", type=float, default=45.0, help="scheduled hours per week (Mon-Fri 15-22, Sat-Sun 14-19)")
    p.add_argument("--ipv4-month", type=float, default=0.0, help="retained Primary IPv4 EUR/month (verify)")
    p.add_argument("--vat-pct", type=float, default=0.0, help="VAT %% to apply (verify)")
    p.add_argument("--eur-usd", type=float, default=None, help="EUR->USD rate (verify)")
    p.add_argument("--budget-usd", type=float, default=3.0)
    args = p.parse_args()

    hours_month = args.hours_week * 52.0 / 12.0
    compute = args.hourly * hours_month
    subtotal = compute + args.ipv4_month
    total_eur = subtotal * (1.0 + args.vat_pct / 100.0)

    print("Scheduled hours/week : %.1f" % args.hours_week)
    print("Hours/month          : %.1f" % hours_month)
    print("Server  (EUR/month)  : %.2f  (%.4f/h)" % (compute, args.hourly))
    print("IPv4    (EUR/month)  : %.2f" % args.ipv4_month)
    print("Subtotal (EUR/month) : %.2f" % subtotal)
    print("Total incl. VAT      : %.2f EUR" % total_eur)

    if args.eur_usd:
        total_usd = total_eur * args.eur_usd
        print("Total                : %.2f USD (rate %.4f)" % (total_usd, args.eur_usd))
        verdict = "WITHIN" if total_usd <= args.budget_usd else "ABOVE"
        print("%s budget (US$%.2f)" % (verdict, args.budget_usd))
        if total_usd > args.budget_usd:
            print("-> reduce hours/week, drop the retained IPv4, or choose a smaller plan;"
                  " re-verify rates before deciding.")
    else:
        print("Provide --eur-usd to compare against the US$%.2f budget." % args.budget_usd)

    print("\nExcluded (verify separately): snapshots, extra volumes, traffic overage, AWS.")
    print("This design adds none of those by default; models are re-downloaded each window.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
