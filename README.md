# SGU cache warmer

Every night, requests every page in `https://www.sgu.edu/sitemap_index.xml` as a desktop, a mobile and a tablet visitor, so that Pagely's page cache (PressCACHE / ARES gateway) holds a copy of each page. It then emails a report.

- About 4,300 pages x 3 device variants = about 12,800 requests, 4 at a time, roughly 3 hours.
- Runs at 06:00 UTC (2am Grenada / US Eastern) on GitHub Actions.
- Identifies itself as `SGU-CacheWarmer/1.0` in the user agent.
- Safety: one retry per failed request. If 20%+ of recent requests fail, it pauses 5 minutes (up to 3 times), then aborts. It stops after 5 hours regardless.

## Pagely cache facts (verified 30 Oct 2026)

- **Cache lifetime and stale copies.** Pagely has set a 24h cache lifetime for the whole site and enabled **serve stale on error**.
- **Cache key by device.** The cache key includes a *device class* worked out from the User-Agent (`standard`, `mobile` or `tablet`), not the full User-Agent string. The `X-Gateway-Cache-Key` response header shows it.
  - Chrome, Firefox, Edge, Safari and desktop Googlebot share `standard`.
  - iPhone, Android phones, Android tablets and Googlebot Smartphone share `mobile`.
  - iPad is `tablet`.
  - Each warmer user agent maps to one class, which is why the script sends three requests per page.
- **No warm-up or refresh method.** Pagely has no built-in cache warm-up and no header that forces a refresh.

## Setup (one time)

1. **Create a GitHub repository** and push this folder.
2. **Add the email secrets.** Go to Settings → Secrets and variables → Actions → New repository secret:

   | Secret | Example |
   |---|---|
   | `SMTP_HOST` | `smtp.office365.com`, `smtp.sendgrid.net`, `smtp.gmail.com` |
   | `SMTP_PORT` | `587` (STARTTLS) or `465` (SSL) |
   | `SMTP_USER` / `SMTP_PASS` | Mailbox login or API key (SendGrid: user `apikey`) |
   | `MAIL_FROM` | `alerts@parsus.com` |
   | `MAIL_TO` | One or more addresses, comma-separated |
   | `WARM_HEADER` *(optional)* | Only if a cache-refresh header is ever agreed with Pagely, e.g. `X-Cache-Warm: <secret>` |

   Microsoft 365 often has SMTP AUTH turned off. If login fails, use SendGrid, Mailgun or Amazon SES, or have IT enable SMTP AUTH for the sending mailbox.
3. **Test it.** Go to Actions → "Warm SGU page cache" → Run workflow, set limit to `10`, and check that the email arrives. Then run it once with `0` to warm the whole site.

## The daily report

- **Subject line verdict:** `OK`, `COMPLETED WITH ERRORS`, `INCOMPLETE` or `ABORTED`.
- **Counts:** HTTP status counts, cache status before each request, errors (first 50 listed), the slowest pages, and pages Pagely refuses to cache.
- **Full CSV:** attached to each run under Actions → run → Artifacts, kept for 30 days.
- **If the job itself fails to start or crashes,** GitHub also emails the repository owner (Settings → Notifications → Actions).

**How to read "cache before request":**
- `MISS` or `EXPIRED`: the page was cold, and it is now cached.
- `HIT`: the page was already cached, so it was **not** refreshed (see below).

## Expiry timing

A cache HIT doesn't reset the 24h clock, and Pagely has no refresh header. Some pages will therefore be reached a few minutes *before* they expire: they get a HIT, expire shortly afterwards, and wait for the next visitor or the next night's run.

This is acceptable because Pagely now **serves stale on error**:
- The expired copy is still stored. If the server is overloaded, visitors get that stale copy instead of a 503.
- What matters for outage protection is that every page has *a* copy, fresh or stale, and the nightly run guarantees that.
- The cost is speed only: the first visitor to an expired page waits about 3s while it is rebuilt.

If the reports show many HITs every night and you want every page fresh, ask Pagely for a 22–23h lifetime instead of 24h. Every page would then have expired by the next run and would be refreshed.

## Things to know

- **GitHub Actions minutes.** About 3 hours a night is about 5,500 minutes a month. Public repositories get unlimited free minutes. Private repositories on the Free plan include 2,000, so set a budget. To cut the time, set `VARIANTS: "desktop,mobile"` (tablet traffic is small) or raise `CONCURRENCY`. Only raise `CONCURRENCY` with Pagely's OK, because it means more simultaneous page builds.
- **Future bot rules.** GitHub runners run on Azure (ASN 8075). If a firewall or Cloudflare rule challenges cloud-hosted traffic, exempt requests whose user agent contains `SGU-CacheWarmer`.
- **Local test:** `LIMIT=10 python warm.py` (the email is skipped if `SMTP_HOST` isn't set).
