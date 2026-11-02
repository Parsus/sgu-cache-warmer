"""Nightly cache warmer for www.sgu.edu (Pagely PressCACHE / ARES gateway).

Reads every page URL from the sitemap index, requests each one as a desktop,
mobile and tablet visitor (Pagely caches these separately), then writes a
report and emails a summary.

Standard library only. Configuration comes from environment variables; see README.md.
"""
import csv
import html
import os
import re
import smtplib
import ssl
import sys
import threading
import time
import urllib.error
import urllib.request
from collections import Counter, deque
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from email.message import EmailMessage
from pathlib import Path

SITEMAP_INDEX = os.environ.get("SITEMAP_INDEX", "https://www.sgu.edu/sitemap_index.xml")
CONCURRENCY = int(os.environ.get("CONCURRENCY", "4"))
MAX_RUNTIME_MIN = int(os.environ.get("MAX_RUNTIME_MIN", "300"))  # stop before morning traffic
TIMEOUT_S = int(os.environ.get("TIMEOUT_S", "60"))
SLOW_S = float(os.environ.get("SLOW_S", "10"))
LIMIT = int(os.environ.get("LIMIT", "0"))  # >0 = only warm the first N URLs (testing)
WARM_HEADER = os.environ.get("WARM_HEADER", "")  # optional "Name: value" cache-refresh header agreed with Pagely
REPORT_DIR = Path(os.environ.get("REPORT_DIR", "report"))

# Pagely's cache key has a device class (standard / mobile / tablet) derived from the
# User-Agent; each agent below maps to one class (checked via X-Gateway-Cache-Key).
ALL_AGENTS = {
    "desktop": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) SGU-CacheWarmer/1.0",
    "mobile": "Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) Mobile SGU-CacheWarmer/1.0",
    "tablet": "Mozilla/5.0 (iPad; CPU OS 18_0 like Mac OS X) SGU-CacheWarmer/1.0",
}
VARIANTS = [v.strip() for v in os.environ.get("VARIANTS", "desktop,mobile,tablet").split(",") if v.strip()]
AGENTS = {v: ALL_AGENTS[v] for v in VARIANTS}
SKIP_EXT = re.compile(r"\.(jpe?g|png|gif|webp|svg|pdf)$", re.I)
LOC = re.compile(r"<loc>\s*([^<\s]+)\s*</loc>")

# Circuit breaker: if the site starts failing, back off instead of piling on.
WINDOW, FAIL_RATIO, PAUSE_S, MAX_PAUSES = 50, 0.2, 300, 3
recent = deque(maxlen=WINDOW)
lock = threading.Lock()
pause_until = 0.0
pauses = 0
aborted = ""


def fetch(url, agent, timeout=TIMEOUT_S):
    req = urllib.request.Request(url, headers={"User-Agent": agent, "Accept-Encoding": "identity"})
    if WARM_HEADER:
        name, _, value = WARM_HEADER.partition(":")
        req.add_header(name.strip(), value.strip())
    start = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            r.read()
            return r.status, r.headers.get("X-Gateway-Cache-Status", ""), r.headers.get("X-Gateway-Skip-Cache", ""), time.monotonic() - start, ""
    except urllib.error.HTTPError as e:
        return e.code, e.headers.get("X-Gateway-Cache-Status", ""), "", time.monotonic() - start, ""
    except Exception as e:  # timeout, connection reset, DNS...
        return 0, "", "", time.monotonic() - start, type(e).__name__


def sitemap_urls():
    index = urllib.request.urlopen(urllib.request.Request(SITEMAP_INDEX, headers={"User-Agent": AGENTS["desktop"]}), timeout=TIMEOUT_S).read().decode()
    urls = []
    for sm in LOC.findall(index):
        body = urllib.request.urlopen(urllib.request.Request(sm, headers={"User-Agent": AGENTS["desktop"]}), timeout=TIMEOUT_S).read().decode()
        urls += [u for u in LOC.findall(body) if not SKIP_EXT.search(u)]
    return list(dict.fromkeys(urls))


def warm(url, variant, deadline):
    global pause_until, pauses, aborted
    while True:
        with lock:
            if aborted or time.time() > deadline:
                return None
            wait = pause_until - time.time()
        if wait <= 0:
            break
        time.sleep(min(wait, 10))

    status, cache, skip, secs, err = fetch(url, AGENTS[variant])
    if status == 0 or status >= 500:  # one retry for transient failures
        time.sleep(5)
        status, cache, skip, secs, err = fetch(url, AGENTS[variant])

    failed = status == 0 or status >= 500
    with lock:
        recent.append(failed)
        if len(recent) == WINDOW and sum(recent) / WINDOW >= FAIL_RATIO and time.time() >= pause_until:
            pauses += 1
            recent.clear()
            if pauses > MAX_PAUSES:
                aborted = f"Aborted: error rate stayed above {FAIL_RATIO:.0%} after {MAX_PAUSES} pauses"
            else:
                pause_until = time.time() + PAUSE_S
                print(f"High error rate, pausing {PAUSE_S}s (pause {pauses}/{MAX_PAUSES})", flush=True)
    return {"url": url, "variant": variant, "status": status, "cache": cache or "-", "skip_cache": skip, "seconds": round(secs, 2), "error": err}


def summarize(rows, total_jobs, started, finished):
    done = len(rows)
    status = Counter(r["status"] for r in rows)
    cache = Counter(r["cache"] for r in rows)
    errors = [r for r in rows if r["status"] == 0 or r["status"] >= 400]
    slow = sorted((r for r in rows if r["seconds"] >= SLOW_S), key=lambda r: -r["seconds"])
    uncacheable = [r for r in rows if r["skip_cache"] == "1"]
    mins = (finished - started).total_seconds() / 60

    if aborted:
        verdict = "ABORTED"
    elif done < total_jobs:
        verdict = "INCOMPLETE"
    elif errors:
        verdict = "COMPLETED WITH ERRORS"
    else:
        verdict = "OK"

    s = {
        "verdict": verdict,
        "subject": f"[SGU cache warmer] {verdict}: {done:,}/{total_jobs:,} requests, {len(errors)} errors ({started:%Y-%m-%d})",
        "lines": [
            ("Result", verdict + (f" ({aborted})" if aborted else "")),
            ("Started / finished (UTC)", f"{started:%Y-%m-%d %H:%M} / {finished:%H:%M} ({mins:.0f} min)"),
            ("Requests completed", f"{done:,} of {total_jobs:,} ({total_jobs // max(len(AGENTS), 1):,} pages x {' + '.join(AGENTS)})"),
            ("HTTP status", ", ".join(f"{k}: {v:,}" for k, v in sorted(status.items()))),
            ("Cache before request", ", ".join(f"{k}: {v:,}" for k, v in cache.most_common())),
            ("Errors (4xx/5xx/timeouts)", f"{len(errors):,}"),
            (f"Slow pages (>= {SLOW_S:.0f}s)", f"{len(slow):,}"),
            ("Pages Pagely will not cache", f"{len(uncacheable):,}"),
            ("Circuit-breaker pauses", str(pauses)),
        ],
        "errors": errors[:50],
        "slow": slow[:20],
    }
    return s


def table(rows, cols):
    head = "".join(f"<th align='left'>{c}</th>" for c in cols)
    body = "".join("<tr>" + "".join(f"<td>{html.escape(str(r[c]))}</td>" for c in cols) + "</tr>" for r in rows)
    return f"<table border='1' cellpadding='4' cellspacing='0' style='border-collapse:collapse;font-size:13px'><tr>{head}</tr>{body}</table>"


def render_html(s, run_url):
    color = {"OK": "#1a7f37", "COMPLETED WITH ERRORS": "#9a6700"}.get(s["verdict"], "#cf222e")
    out = [f"<h2 style='color:{color};font-family:sans-serif'>SGU cache warmer: {s['verdict']}</h2>",
           "<table cellpadding='4' style='font-family:sans-serif;font-size:14px'>"]
    out += [f"<tr><td><b>{k}</b></td><td>{html.escape(v)}</td></tr>" for k, v in s["lines"]]
    out.append("</table>")
    cols = ["url", "variant", "status", "error", "seconds"]
    if s["errors"]:
        out += ["<h3 style='font-family:sans-serif'>Errors (first 50)</h3>", table(s["errors"], cols)]
    if s["slow"]:
        out += ["<h3 style='font-family:sans-serif'>Slowest pages (top 20)</h3>", table(s["slow"], cols)]
    out.append("<p style='font-family:sans-serif;font-size:13px'>Cache before request: MISS/EXPIRED means the page was cold and has now been cached. "
               "A high HIT count means pages were still cached and were <i>not</i> refreshed (see README, \"expiry timing\").</p>")
    if run_url:
        out.append(f"<p style='font-family:sans-serif;font-size:13px'>Full CSV report: <a href='{run_url}'>{run_url}</a> (Artifacts)</p>")
    return "\n".join(out)


def send_email(subject, body_html):
    host = os.environ.get("SMTP_HOST")
    if not host:
        print("SMTP_HOST not set; skipping email")
        return
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = os.environ["MAIL_FROM"]
    msg["To"] = os.environ["MAIL_TO"]
    msg.set_content("This report is HTML; open it in an HTML-capable mail client.")
    msg.add_alternative(body_html, subtype="html")
    port = int(os.environ.get("SMTP_PORT", "587"))
    ctx = ssl.create_default_context()
    if port == 465:
        server = smtplib.SMTP_SSL(host, port, context=ctx, timeout=60)
    else:
        server = smtplib.SMTP(host, port, timeout=60)
        server.starttls(context=ctx)
    with server:
        if os.environ.get("SMTP_USER"):
            server.login(os.environ["SMTP_USER"], os.environ["SMTP_PASS"])
        server.send_message(msg)
    print(f"Report emailed to {msg['To']}")


def main():
    global aborted
    started = datetime.now(timezone.utc)
    deadline = time.time() + MAX_RUNTIME_MIN * 60
    rows = []
    run_url = ""
    if os.environ.get("GITHUB_RUN_ID"):
        run_url = f"{os.environ['GITHUB_SERVER_URL']}/{os.environ['GITHUB_REPOSITORY']}/actions/runs/{os.environ['GITHUB_RUN_ID']}"

    try:
        urls = sitemap_urls()
    except Exception as e:
        urls = []
        aborted = f"Aborted: could not read sitemap ({type(e).__name__}: {e})"
    if LIMIT:
        urls = urls[:LIMIT]
    jobs = [(u, v) for u in urls for v in AGENTS]
    print(f"{len(urls):,} pages, {len(jobs):,} requests, concurrency {CONCURRENCY}", flush=True)

    with ThreadPoolExecutor(CONCURRENCY) as pool:
        futures = [pool.submit(warm, u, v, deadline) for u, v in jobs]
        for i, f in enumerate(as_completed(futures), 1):
            r = f.result()
            if r:
                rows.append(r)
            if i % 500 == 0:
                print(f"{i:,}/{len(jobs):,}", flush=True)

    finished = datetime.now(timezone.utc)
    if not aborted and len(rows) < len(jobs) and jobs:
        aborted = f"Stopped at the {MAX_RUNTIME_MIN}-minute limit"

    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    with open(REPORT_DIR / "results.csv", "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=["url", "variant", "status", "cache", "skip_cache", "seconds", "error"])
        w.writeheader()
        w.writerows(rows)

    s = summarize(rows, len(jobs), started, finished)
    body = render_html(s, run_url)
    (REPORT_DIR / "summary.html").write_text(body, encoding="utf-8")
    print(s["subject"])
    for k, v in s["lines"]:
        print(f"  {k}: {v}")

    send_email(s["subject"], body)
    return 0 if s["verdict"] in ("OK", "COMPLETED WITH ERRORS") else 1


if __name__ == "__main__":
    sys.exit(main())
