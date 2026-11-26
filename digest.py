"""News digest: collects your feeds and either emails them or builds a web page.

  python digest.py email          send the email if one is due now (used by the schedule)
  python digest.py email --force  send an email right now ("send now")
  python digest.py page           build the always-up-to-date page into public/index.html
  python digest.py preview        write preview.html locally, change nothing
"""
import html
import json
import os
import smtplib
import socket
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from urllib.parse import quote_plus
from zoneinfo import ZoneInfo

import feedparser
import yaml

ROOT = Path(__file__).resolve().parent
STATE_FILE = ROOT / "state" / "seen.json"
UA = "Mozilla/5.0 (news-digest; +https://github.com)"
KINDS = [("written", "📰", "Written"), ("audio", "🎧", "Audio"), ("video", "▶️", "Video")]
SLOT_GRACE = timedelta(hours=3)   # how late a scheduled email may still go out
SEEN_KEEP = timedelta(days=4)     # how long to remember what was already sent

socket.setdefaulttimeout(20)


# ---------------------------------------------------------------- config & state

def load_config():
    with open(ROOT / "feeds.yaml", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    s = cfg.get("settings", {})
    s.setdefault("timezone", "Europe/Paris")
    s.setdefault("email_hours", [6, 12, 18])
    s.setdefault("default_max", 3)
    s.setdefault("lookback_hours", 24)
    return s, cfg["sources"]


def load_state():
    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        data = {}
    data.setdefault("seen", {})
    return data


def save_state(state, now):
    cutoff = now - SEEN_KEEP
    state["seen"] = {k: v for k, v in state["seen"].items()
                     if datetime.fromisoformat(v) > cutoff}
    STATE_FILE.parent.mkdir(exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=1, ensure_ascii=False), encoding="utf-8")


# ---------------------------------------------------------------- fetching

def candidate_urls(src):
    if "youtube" in src:
        cid = src["youtube"]
        # "UULF" playlist = long videos only (no Shorts); fall back to the full channel feed.
        return [f"https://www.youtube.com/feeds/videos.xml?playlist_id=UULF{cid[2:]}",
                f"https://www.youtube.com/feeds/videos.xml?channel_id={cid}"]
    if "google_news" in src:
        q = quote_plus(f"when:1d {src['google_news']}")
        if src.get("lang") == "fr":
            return [f"https://news.google.com/rss/search?q={q}&hl=fr&gl=FR&ceid=FR:fr"]
        return [f"https://news.google.com/rss/search?q={q}&hl=en-US&gl=US&ceid=US:en"]
    return [src["url"]]


def entry_time(e):
    t = e.get("published_parsed") or e.get("updated_parsed")
    return datetime(*t[:6], tzinfo=timezone.utc) if t else None


def thumbnail(e):
    for key in ("media_thumbnail", "media_content"):
        items = e.get(key)
        if items and items[0].get("url"):
            return items[0]["url"]
    return None


def clean_title(title, via_google):
    title = " ".join((title or "(no title)").split())
    if via_google and " - " in title:      # Google News appends " - Source"
        title = title.rsplit(" - ", 1)[0]
    return title


def fetch_source(src, since):
    last_error = "no entries"
    for url in candidate_urls(src):
        feed = feedparser.parse(url, agent=UA)
        if not feed.entries:
            last_error = str(feed.get("bozo_exception") or f"HTTP {feed.get('status', '?')}")
            continue
        via_google = "news.google.com" in url
        items = []
        for e in feed.entries:
            link = e.get("link", "")
            title = clean_title(e.get("title"), via_google)
            if "/shorts/" in link or "#shorts" in title.lower():
                continue
            t = entry_time(e)
            if t and t < since:
                continue
            items.append({"source": src["name"], "kind": src.get("kind", "written"),
                          "title": title, "link": link, "time": t,
                          "thumb": thumbnail(e) if src.get("kind") == "video" else None})
        items.sort(key=lambda i: i["time"] or since, reverse=True)
        return items, None
    return [], last_error


def collect(settings, sources, now):
    since = now - timedelta(hours=settings["lookback_hours"])
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda s: fetch_source(s, since), sources))
    out, errors = [], []
    for src, (items, err) in zip(sources, results):
        if err:
            errors.append(f"{src['name']}: {err}")
        out.append((src, items))
    return out, errors


def select(collected, settings, skip=None):
    """Group items by kind -> source, applying per-source caps. skip = links to leave out."""
    grouped = {k: [] for k, _, _ in KINDS}
    for src, items in collected:
        if skip is not None:
            items = [i for i in items if i["link"] not in skip]
        items = items[: src.get("max", settings["default_max"])]
        if items:
            grouped.setdefault(src.get("kind", "written"), []).append((src["name"], items))
    return grouped


# ---------------------------------------------------------------- rendering

def fmt_time(t, tz, now):
    if not t:
        return ""
    local = t.astimezone(tz)
    day = local.date()
    if day == now.astimezone(tz).date():
        return local.strftime("%H:%M")
    if day == (now.astimezone(tz) - timedelta(days=1)).date():
        return "yesterday " + local.strftime("%H:%M")
    return local.strftime("%d %b %H:%M")


def render(grouped, errors, tz, now, heading, page_links=None, new_links=None):
    esc = html.escape
    total = sum(len(items) for groups in grouped.values() for _, items in groups)
    p = ["<!DOCTYPE html><html><head><meta charset='utf-8'>",
         "<meta name='viewport' content='width=device-width,initial-scale=1'>",
         f"<title>{esc(heading)}</title></head>",
         "<body style='margin:0;background:#f4f2ee;font-family:-apple-system,Segoe UI,Helvetica,Arial,sans-serif;color:#1d1d1f'>",
         "<div style='max-width:680px;margin:auto;padding:20px 16px'>",
         f"<h1 style='font-family:Georgia,serif;font-size:24px;margin:0 0 4px'>{esc(heading)}</h1>",
         f"<p style='color:#777;font-size:13px;margin:0 0 12px'>{total} items · "
         f"updated {now.astimezone(tz):%A %d %B, %H:%M}</p>"]
    if page_links:
        btn = ("display:inline-block;padding:9px 14px;margin:0 8px 8px 0;border-radius:8px;"
               "background:#1d1d1f;color:#fff;text-decoration:none;font-size:14px")
        p.append("<p style='margin:0 0 8px'>" + "".join(
            f"<a href='{esc(url)}' style='{btn}'>{esc(label)}</a>" for label, url in page_links) + "</p>")
    # quick jump links
    jumps = [f"<a href='#{k}' style='color:#555'>{icon} {label}</a>"
             for k, icon, label in KINDS if grouped.get(k)]
    if jumps:
        p.append("<p style='font-size:14px;margin:0 0 8px'>" + " &nbsp;·&nbsp; ".join(jumps) + "</p>")

    for kind, icon, label in KINDS:
        groups = grouped.get(kind)
        if not groups:
            continue
        p.append(f"<h2 id='{kind}' style='font-family:Georgia,serif;font-size:20px;"
                 f"margin:28px 0 4px;border-bottom:2px solid #1d1d1f;padding-bottom:4px'>{icon} {label}</h2>")
        for name, items in groups:
            p.append(f"<h3 style='font-size:13px;text-transform:uppercase;letter-spacing:.05em;"
                     f"color:#8a5a00;margin:16px 0 6px'>{esc(name)}</h3>")
            for i in items:
                badge = ""
                if new_links is not None and i["link"] in new_links:
                    badge = ("<span style='background:#d93025;color:#fff;font-size:10px;font-weight:600;"
                             "padding:1px 5px;border-radius:3px;margin-right:6px'>NEW</span>")
                when = fmt_time(i["time"], tz, now)
                text = (f"{badge}<a href='{esc(i['link'])}' style='color:#1a4d8f;text-decoration:none;"
                        f"font-size:15px;line-height:1.35'>{esc(i['title'])}</a>"
                        f"<span style='color:#999;font-size:12px'> &nbsp;{when}</span>")
                if i.get("thumb"):
                    p.append("<table cellpadding='0' cellspacing='0' style='margin:6px 0;border-collapse:collapse'><tr>"
                             f"<td style='width:128px;vertical-align:top;padding-right:10px'>"
                             f"<a href='{esc(i['link'])}'><img src='{esc(i['thumb'])}' width='120' "
                             f"style='display:block;border-radius:6px' alt=''></a></td>"
                             f"<td style='vertical-align:top'>{text}</td></tr></table>")
                else:
                    p.append(f"<p style='margin:6px 0'>{text}</p>")

    if total == 0:
        p.append("<p style='color:#777'>Nothing new from your sources since last time.</p>")
    if errors:
        p.append("<p style='color:#a33;font-size:12px;margin-top:28px'>Couldn't reach: "
                 + esc("; ".join(errors)) + "</p>")
    p.append("</div></body></html>")
    return "\n".join(p), total


# ---------------------------------------------------------------- email

def current_slot(now_local, hours):
    """Most recent scheduled send time at or before now."""
    slots = []
    for h in hours:
        s = now_local.replace(hour=h, minute=0, second=0, microsecond=0)
        slots += [s, s - timedelta(days=1)]
    return max(s for s in slots if s <= now_local)


def send_email(subject, body):
    user = os.environ["GMAIL_USER"]
    pwd = os.environ["GMAIL_APP_PASSWORD"]
    to = os.environ.get("DIGEST_TO") or user
    msg = MIMEMultipart("alternative")
    msg["Subject"], msg["From"], msg["To"] = subject, user, to
    msg.attach(MIMEText(body, "html", "utf-8"))
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as s:
        s.login(user, pwd)
        s.send_message(msg)


def run_email(force):
    settings, sources = load_config()
    tz = ZoneInfo(settings["timezone"])
    now = datetime.now(timezone.utc)
    now_local = now.astimezone(tz)
    state = load_state()

    slot = current_slot(now_local, settings["email_hours"])
    slot_key = slot.strftime("%Y-%m-%d %H:00")
    if not force:
        if state.get("last_slot") == slot_key:
            print(f"Already sent for {slot_key}; nothing to do.")
            return
        if now_local - slot > SLOT_GRACE:
            print(f"Not a send time (last slot {slot_key} was too long ago).")
            return

    collected, errors = collect(settings, sources, now)
    grouped = select(collected, settings, skip=set(state["seen"]))
    heading = f"Your news · {now_local:%H:%M}"
    body, total = render(grouped, errors, tz, now, heading)
    send_email(f"{heading} · {total} new", body)

    for groups in grouped.values():
        for _, items in groups:
            for i in items:
                state["seen"][i["link"]] = now.isoformat()
    if not force:
        state["last_slot"] = slot_key
    state["last_sent"] = now.isoformat()
    save_state(state, now)
    print(f"Sent {total} items.")


# ---------------------------------------------------------------- page / preview

def run_page(out_path, with_buttons=True):
    settings, sources = load_config()
    tz = ZoneInfo(settings["timezone"])
    now = datetime.now(timezone.utc)
    state = load_state()
    collected, errors = collect(settings, sources, now)
    grouped = select(collected, settings)
    links = None
    repo = os.environ.get("GITHUB_REPOSITORY")
    if with_buttons and repo:
        base = f"https://github.com/{repo}/actions/workflows"
        links = [("↻ Refresh this page", f"{base}/page.yml"),
                 ("✉ Email me now", f"{base}/email.yml")]
    body, total = render(grouped, errors, tz, now, "Your news", page_links=links,
                         new_links=None if not state["seen"] else
                         {i["link"] for g in grouped.values() for _, its in g for i in its
                          if i["link"] not in state["seen"]})
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(body, encoding="utf-8")
    print(f"Wrote {out_path} ({total} items, {len(errors)} unreachable)")


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "preview"
    if mode == "email":
        run_email(force="--force" in sys.argv)
    elif mode == "page":
        run_page(ROOT / "public" / "index.html")
    elif mode == "preview":
        run_page(ROOT / "preview.html", with_buttons=False)
    else:
        sys.exit(__doc__)
