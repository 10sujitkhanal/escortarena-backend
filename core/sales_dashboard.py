"""Admin → Sales → Sales dashboard: money in for any day, month, year or own date range, with charts and breakdowns.

All amounts are paid orders (Order.status = paid, by paid_at), in minor units in the database and shown as 0.00.
An order's add-ons are counted as "Add-ons" (their prices are stored on the order); the rest of the order counts for its
product type (Ads plan, VIP plan or Coins).
"""

import calendar
import csv
from collections import defaultdict
from datetime import date, datetime, time, timedelta

from django.http import HttpResponse
from django.urls import reverse
from django.utils import timezone

from .models import Country, Order, SiteSettings

TYPES = [("plan", "Ads plan", "#e94fc2"), ("addons", "Add-ons", "#f59e0b"), ("membership", "VIP plan", "#8b5cf6"),
         ("diamonds", "Coins", "#22c55e")]
PRESETS = [("today", "Today"), ("yesterday", "Yesterday"), ("7d", "Last 7 days"), ("30d", "Last 30 days"),
           ("month", "This month"), ("last_month", "Last month"), ("year", "This year"), ("last_year", "Last year"),
           ("all", "All time")]


def money(minor) -> str:
    return f"{(minor or 0) / 100:,.2f}".replace(",", " ")


def _start(d: date) -> datetime:
    return timezone.make_aware(datetime.combine(d, time.min))


def period(params) -> dict:
    """{key, label, start, end (exclusive), first_day, last_day} from ?p=… or ?from=YYYY-MM-DD&to=YYYY-MM-DD."""
    today = timezone.localdate()
    key = params.get("p") or ("custom" if params.get("from") else "30d")

    def parse(v, default):
        try:
            return date.fromisoformat(v)
        except (TypeError, ValueError):
            return default

    if key == "custom":
        first = parse(params.get("from"), today - timedelta(days=29))
        last = parse(params.get("to"), today)
        if last < first:
            first, last = last, first
        label = f"{first:%d %b %Y} – {last:%d %b %Y}" if first != last else f"{first:%A %d %B %Y}"
    elif key == "today":
        first = last = today
        label = "Today"
    elif key == "yesterday":
        first = last = today - timedelta(days=1)
        label = "Yesterday"
    elif key == "7d":
        first, last, label = today - timedelta(days=6), today, "Last 7 days"
    elif key == "month":
        first, last, label = today.replace(day=1), today, f"{today:%B %Y}"
    elif key == "last_month":
        last = today.replace(day=1) - timedelta(days=1)
        first, label = last.replace(day=1), f"{last:%B %Y}"
    elif key == "year":
        first, last, label = today.replace(month=1, day=1), today, str(today.year)
    elif key == "last_year":
        first, last = date(today.year - 1, 1, 1), date(today.year - 1, 12, 31)
        label = str(today.year - 1)
    elif key == "all":
        oldest = Order.objects.filter(status=Order.Status.PAID).order_by("paid_at").values_list("paid_at", flat=True).first()
        first = timezone.localdate(oldest) if oldest else today
        last, label = today, "All time"
    else:  # 30d
        key, first, last, label = "30d", today - timedelta(days=29), today, "Last 30 days"
    return {"key": key, "label": label, "first_day": first, "last_day": last,
            "start": _start(first), "end": _start(last + timedelta(days=1)), "days": (last - first).days + 1}


def _split(order) -> dict:
    """Money of one order per type: add-ons separately, the rest for the product's kind."""
    addons = sum(int(a.get("price") or 0) for a in (order.addons or []))
    addons = min(addons, order.amount)
    return {order.product.kind: order.amount - addons, **({"addons": addons} if addons else {})}


def _paid(start, end):
    return (Order.objects.filter(status=Order.Status.PAID, paid_at__gte=start, paid_at__lt=end)
            .select_related("product", "user"))


def _totals(orders) -> dict:
    total = sum(o.amount for o in orders)
    buyers = len({o.user_id for o in orders})
    return {"amount": total, "orders": len(orders), "buyers": buyers, "average": total // len(orders) if orders else 0}


def _change(now_value, before) -> dict | None:
    if not before:
        return None if not now_value else {"text": "new", "up": True}
    pct = (now_value - before) / before * 100
    return {"text": f"{pct:+.0f}%", "up": pct >= 0}


def _bars(orders, per: dict, first: date, days: int) -> tuple[list, str]:
    """Chart columns. Day by day up to 62 days, then month by month, then year by year."""
    unit = "day" if days <= 62 else ("month" if days <= 3 * 366 else "year")
    step = unit
    buckets: dict = defaultdict(lambda: defaultdict(int))
    for o in orders:
        d = timezone.localtime(o.paid_at).date()
        k = d if unit == "day" else (d.replace(day=1) if unit == "month" else d.replace(month=1, day=1))
        for t, v in per[o.pk].items():
            buckets[k][t] += v
    keys, d = [], first if unit == "day" else (first.replace(day=1) if unit == "month" else first.replace(month=1, day=1))
    last = first + timedelta(days=days - 1)
    while d <= last:
        keys.append(d)
        if step == "day":
            d += timedelta(days=1)
        elif step == "month":
            d = (d.replace(day=28) + timedelta(days=4)).replace(day=1)
        else:
            d = d.replace(year=d.year + 1)
    peak = max([sum(buckets[k].values()) for k in keys] + [1])
    fmt = {"day": "%d %b", "month": "%b %Y", "year": "%Y"}[unit]
    short = {"day": "%d/%m", "month": "%b", "year": "%Y"}[unit]
    cols = []
    for k in keys:
        parts = [{"key": t, "label": label, "color": color, "value": buckets[k].get(t, 0),
                  "h": round(buckets[k].get(t, 0) / peak * 100, 1)} for t, label, color in TYPES]
        total = sum(p["value"] for p in parts)
        cols.append({"label": k.strftime(fmt), "short": k.strftime(short), "total": money(total), "parts": parts,
                     "tip": " · ".join(f"{p['label']} {money(p['value'])}" for p in parts if p["value"]) or "nothing"})
    return cols, unit


def year_overview(year: int) -> dict:
    """12 months of a year next to the year before."""
    def months(y):
        out = [0] * 12
        for o in _paid(_start(date(y, 1, 1)), _start(date(y + 1, 1, 1))):
            out[timezone.localtime(o.paid_at).month - 1] += o.amount
        return out
    this, prev = months(year), months(year - 1)
    peak = max(this + prev + [1])
    return {
        "year": year, "total": money(sum(this)), "prev_total": money(sum(prev)), "change": _change(sum(this), sum(prev)),
        "months": [{"name": calendar.month_abbr[i + 1], "this": money(this[i]), "prev": money(prev[i]),
                    "h_this": round(this[i] / peak * 100, 1), "h_prev": round(prev[i] / peak * 100, 1)} for i in range(12)],
    }


def build(params) -> dict:
    per_info = period(params)
    start, end = per_info["start"], per_info["end"]
    orders = list(_paid(start, end).order_by("paid_at"))
    per = {o.pk: _split(o) for o in orders}
    totals = _totals(orders)

    span = end - start
    before = list(_paid(start - span, start))
    prev = _totals(before)

    by_type = defaultdict(int)
    for split in per.values():
        for t, v in split.items():
            by_type[t] += v
    type_total = sum(by_type.values()) or 1
    types = [{"key": t, "label": label, "color": color, "amount": money(by_type.get(t, 0)),
              "pct": round(by_type.get(t, 0) / type_total * 100), "count": sum(1 for s in per.values() if s.get(t))}
             for t, label, color in TYPES]

    products: dict = defaultdict(lambda: {"amount": 0, "count": 0, "kind": ""})
    for o in orders:
        p = products[o.product_name]
        p["amount"] += o.amount
        p["count"] += 1
        p["kind"] = o.product.get_kind_display()
    top_products = sorted(({"name": k, **v} for k, v in products.items()), key=lambda x: -x["amount"])[:10]
    peak = max([p["amount"] for p in top_products] + [1])
    for p in top_products:
        p["w"], p["amount_text"] = round(p["amount"] / peak * 100), money(p["amount"])

    addon_sales: dict = defaultdict(lambda: {"amount": 0, "count": 0})
    for o in orders:
        for a in o.addons or []:
            row = addon_sales[a.get("name", "Add-on")]
            row["amount"] += int(a.get("price") or 0)
            row["count"] += 1
    addons = sorted(({"name": k, "amount": money(v["amount"]), "count": v["count"]} for k, v in addon_sales.items()),
                    key=lambda x: -x["count"])

    names = {c.code: c for c in Country.objects.all()}
    by_country: dict = defaultdict(int)
    for o in orders:
        by_country[o.user.phone_country or "–"] += o.amount
    peak_c = max(list(by_country.values()) + [1])
    countries = [{"code": k, "name": names[k].name if k in names else k, "flag": names[k].flag if k in names else "",
                  "amount": money(v), "w": round(v / peak_c * 100)}
                 for k, v in sorted(by_country.items(), key=lambda x: -x[1])[:10]]

    buyers: dict = defaultdict(lambda: {"amount": 0, "count": 0})
    for o in orders:
        b = buyers[o.user]
        b["amount"] += o.amount
        b["count"] += 1
    top_buyers = [{"name": u.display_name or u.email, "email": u.email, "advertiser": u.is_advertiser,
                   "url": reverse("admin:core_user_change", args=[u.pk]), "amount": money(v["amount"]), "count": v["count"]}
                  for u, v in sorted(buyers.items(), key=lambda x: -x[1]["amount"])[:10]]

    all_orders = Order.objects.filter(created_at__gte=start, created_at__lt=end)
    statuses = []
    for value, label in Order.Status.choices:
        qs = all_orders.filter(status=value)
        n = qs.count()
        if n:
            statuses.append({"key": value, "label": label, "count": n, "amount": money(sum(qs.values_list("amount", flat=True)))})
    created = all_orders.count()
    conversion = round(totals["orders"] / created * 100) if created else None

    discount = sum(max(0, (o.list_amount or o.amount) - o.amount) for o in orders if o.offer_id)
    offers: dict = defaultdict(int)
    for o in orders:
        if o.offer_id:
            offers[o.offer.title if o.offer else "Offer"] += 1

    chart, unit = _bars(orders, per, per_info["first_day"], per_info["days"])
    today = timezone.localdate()
    try:
        year = int(params.get("year") or per_info["last_day"].year)
    except ValueError:
        year = today.year
    first_year = Order.objects.filter(status=Order.Status.PAID).order_by("paid_at").values_list("paid_at", flat=True).first()
    years = list(range(today.year, (timezone.localdate(first_year).year if first_year else today.year) - 2, -1))

    query = params.copy() if hasattr(params, "copy") else dict(params)
    export_qs = "&".join(f"{k}={v}" for k, v in query.items() if k in ("p", "from", "to"))
    recent = [
        {"url": reverse("admin:core_order_change", args=[o.pk]), "id": o.order_id, "at": o.paid_at, "what": o.product_name,
         "kind": o.product.get_kind_display(), "addons": ", ".join(a.get("name", "") for a in (o.addons or [])),
         "user": o.user.display_name or o.user.email, "user_url": reverse("admin:core_user_change", args=[o.user_id]),
         "amount": money(o.amount), "currency": o.currency}
        for o in sorted(orders, key=lambda x: x.paid_at, reverse=True)[:50]
    ]

    return {
        "period": per_info, "presets": PRESETS, "currency": SiteSettings.load().currency,
        "kpis": [
            {"label": "Money in", "value": money(totals["amount"]), "change": _change(totals["amount"], prev["amount"]),
             "before": money(prev["amount"])},
            {"label": "Paid orders", "value": totals["orders"], "change": _change(totals["orders"], prev["orders"]),
             "before": prev["orders"]},
            {"label": "Buyers", "value": totals["buyers"], "change": _change(totals["buyers"], prev["buyers"]),
             "before": prev["buyers"]},
            {"label": "Average order", "value": money(totals["average"]), "change": _change(totals["average"], prev["average"]),
             "before": money(prev["average"])},
            {"label": "Paid / started", "value": f"{conversion}%" if conversion is not None else "–",
             "sub": f"{totals['orders']} of {created} checkouts"},
            {"label": "Discount given", "value": money(discount), "sub": f"{sum(offers.values())} orders with an offer"},
        ],
        "chart": chart, "unit": unit, "types": types, "top_products": top_products, "addons": addons,
        "countries": countries, "top_buyers": top_buyers, "statuses": statuses,
        "offers": sorted(offers.items(), key=lambda x: -x[1]), "recent": recent, "recent_more": max(0, len(orders) - 50),
        "year_view": year_overview(year), "years": years, "export_qs": export_qs,
    }


def export_csv(params) -> HttpResponse:
    """Every order (all statuses) started or paid in the chosen period, for Excel / accounting."""
    p = period(params)
    rows = (Order.objects.filter(created_at__lt=p["end"]).filter(created_at__gte=p["start"])
            | Order.objects.filter(paid_at__gte=p["start"], paid_at__lt=p["end"])).distinct()
    rows = rows.select_related("user", "product", "ad", "offer").order_by("created_at")
    res = HttpResponse(content_type="text/csv; charset=utf-8")
    res["Content-Disposition"] = f'attachment; filename="sales-{p["first_day"]}-{p["last_day"]}.csv"'
    res.write("﻿")  # Excel: UTF-8
    w = csv.writer(res)
    w.writerow(["Order", "Status", "Created", "Paid", "Type", "Product", "Add-ons", "Add-ons amount", "Amount",
                "Price before offer", "Offer", "Currency", "Customer", "E-mail", "Customer country", "Ad"])
    for o in rows:
        addons_amount = sum(int(a.get("price") or 0) for a in (o.addons or []))
        w.writerow([
            o.order_id, o.get_status_display(), timezone.localtime(o.created_at).strftime("%Y-%m-%d %H:%M"),
            timezone.localtime(o.paid_at).strftime("%Y-%m-%d %H:%M") if o.paid_at else "",
            o.product.get_kind_display(), o.product_name, ", ".join(a.get("name", "") for a in (o.addons or [])),
            money(addons_amount), money(o.amount), money(o.list_amount or o.amount), o.offer.title if o.offer else "",
            o.currency, o.user.display_name or "", o.user.email, o.user.phone_country, o.ad.title if o.ad else "",
        ])
    return res

