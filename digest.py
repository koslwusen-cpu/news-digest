"""News digest: collects your feeds and either emails them or builds a web page.

  python digest.py email          send the email if one is due now (used by the schedule)
  python digest.py email --force  send an email right now ("send now")
  python digest.py page           build the always-up-to-date page into public/index.html
  python digest.py preview        write preview.html locally, change nothing
"""
import html
import json
import os
import re
import smtplib
import socket
import sys
import urllib.request
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
    s.setdefault("page_max", None)   # None = show everything from the lookback window
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
        if items and str(items[0].get("url", "")).startswith("https://"):
            return items[0]["url"]
    return None


def clean_title(title, via_google):
    title = " ".join((title or "(no title)").split())
    if via_google and " - " in title:      # Google News appends " - Source"
        title = title.rsplit(" - ", 1)[0]
    return title


def parse_seconds(value):
    """Podcast durations come as '1234', '20:35' or '1:02:03'."""
    if not value:
        return None
    try:
        parts = [int(float(p)) for p in str(value).strip().split(":")]
    except ValueError:
        return None
    secs = 0
    for p in parts:
        secs = secs * 60 + p
    return secs or None


def fetch_source(src, since):
    last_error = "no entries"
    for url in candidate_urls(src):
        feed = feedparser.parse(url, agent=UA)
        if not feed.entries:
            last_error = str(feed.get("bozo_exception") or f"HTTP {feed.get('status', '?')}")
            continue
        via_google = "news.google.com" in url
        kind = src.get("kind", "written")
        items = []
        for e in feed.entries:
            link = e.get("link", "")
            if not link.startswith(("https://", "http://")):   # only normal web links
                continue
            title = clean_title(e.get("title"), via_google)
            if "/shorts/" in link or "#shorts" in title.lower():
                continue
            t = entry_time(e)
            if t and t < since:
                continue
            items.append({"source": src["name"], "kind": kind,
                          "title": title, "link": link, "time": t,
                          "thumb": thumbnail(e) if kind == "video" else None,
                          "video_id": e.get("yt_videoid"),
                          "seconds": parse_seconds(e.get("itunes_duration"))})
        items.sort(key=lambda i: i["time"] or since, reverse=True)
        return items, None
    return [], last_error


def iso_duration_seconds(text):
    m = re.fullmatch(r"P(?:(\d+)D)?T?(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?", text or "")
    if not m:
        return None
    d, h, mi, s = (int(x or 0) for x in m.groups())
    return d * 86400 + h * 3600 + mi * 60 + s or None


def add_video_durations(collected):
    """Look up YouTube video lengths. Only runs if a YOUTUBE_API_KEY is set."""
    key = os.environ.get("YOUTUBE_API_KEY")
    if not key:
        return
    items = [i for _, its in collected for i in its if i.get("video_id")]
    ids = sorted({i["video_id"] for i in items})
    durations = {}
    for start in range(0, len(ids), 50):
        batch = ",".join(ids[start:start + 50])
        url = ("https://www.googleapis.com/youtube/v3/videos?part=contentDetails"
               f"&id={batch}&key={quote_plus(key)}")
        try:
            with urllib.request.urlopen(url, timeout=20) as r:
                data = json.load(r)
        except Exception as exc:     # a failed lookup just means no lengths shown
            print(f"YouTube lengths unavailable: {exc}")
            return
        for v in data.get("items", []):
            durations[v["id"]] = iso_duration_seconds(v["contentDetails"].get("duration"))
    for i in items:
        i["seconds"] = durations.get(i["video_id"]) or i.get("seconds")


def collect(settings, sources, now):
    since = now - timedelta(hours=settings["lookback_hours"])
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda s: fetch_source(s, since), sources))
    out, errors = [], []
    for src, (items, err) in zip(sources, results):
        if err:
            errors.append(f"{src['name']}: {err}")
        out.append((src, items))
    add_video_durations(out)
    return out, errors


def select(collected, settings, for_email, skip=None):
    """Group items by kind -> [(source name, items, how many to show before 'more')]."""
    grouped = {k: [] for k, _, _ in KINDS}
    for src, items in collected:
        if for_email and src.get("email", True) is False:
            continue
        if skip is not None:
            items = [i for i in items if i["link"] not in skip]
        visible = src.get("max", settings["default_max"])
        if for_email:
            items = items[:visible]
        else:
            cap = src.get("page_max", settings["page_max"])
            items = items if cap is None else items[:max(visible, cap)]
        if items:
            grouped.setdefault(src.get("kind", "written"), []).append((src["name"], items, visible))
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


def fmt_length(secs):
    if not secs:
        return ""
    if secs >= 3600:
        return f"{secs // 3600} h {secs % 3600 // 60:02d}"
    return f"{max(1, round(secs / 60))} min"


def render_item(i, tz, now, new_links):
    esc = html.escape
    nt = " target='_blank' rel='noopener noreferrer'"
    badge = ""
    if new_links is not None and i["link"] in new_links:
        badge = ("<span style='background:#d93025;color:#fff;font-size:10px;font-weight:600;"
                 "padding:1px 5px;border-radius:3px;margin-right:6px'>NEW</span>")
    meta = " · ".join(x for x in (fmt_time(i["time"], tz, now), fmt_length(i.get("seconds"))) if x)
    text = (f"{badge}<a href='{esc(i['link'])}'{nt} style='color:#1a4d8f;text-decoration:none;"
            f"font-size:15px;line-height:1.35'>{esc(i['title'])}</a>"
            f"<span style='color:#999;font-size:12px'> &nbsp;{esc(meta)}</span>")
    if i.get("thumb"):
        return ("<table cellpadding='0' cellspacing='0' style='margin:6px 0;border-collapse:collapse'><tr>"
                f"<td style='width:128px;vertical-align:top;padding-right:10px'>"
                f"<a href='{esc(i['link'])}'{nt}><img src='{esc(i['thumb'])}' width='120' "
                f"style='display:block;border-radius:6px' alt=''></a></td>"
                f"<td style='vertical-align:top'>{text}</td></tr></table>")
    if i["kind"] == "written":   # a dash before each written story, wrapped lines indented
        return (f"<p style='margin:6px 0;padding-left:16px;text-indent:-16px'>"
                f"<span style='color:#8a5a00;display:inline-block;width:16px;text-indent:0'>–</span>{text}</p>")
    return f"<p style='margin:6px 0'>{text}</p>"


PAGE_CSS = (
    ".older-note{display:none;color:#999;font-size:12px;margin:2px 0 6px}"
    ".collapsed .older{display:none}.collapsed .older-note{display:block}"
    ".srchead{display:flex;align-items:baseline;justify-content:space-between;gap:8px;margin:22px 0 6px}"
    ".mini{padding:3px 9px;border-radius:6px;border:1px solid #c9b48a;background:transparent;color:#8a5a00;"
    "font-size:12px;cursor:pointer;white-space:nowrap}"
    ".toc{display:flex;flex-wrap:wrap;gap:6px;margin:4px 0 10px}"
    ".toc a{padding:4px 10px;border-radius:14px;background:#e9e4da;color:#1d1d1f;text-decoration:none;font-size:13px}"
    ".totop{display:block;text-align:right;font-size:13px;color:#8a5a00;text-decoration:none;margin:14px 0 0}"
    "#fab{position:fixed;right:16px;bottom:16px;width:44px;height:44px;border-radius:22px;background:#1d1d1f;"
    "color:#fff;text-align:center;line-height:44px;font-size:20px;text-decoration:none;opacity:.85}"
)

PAGE_JS = (
    "<script>"
    "function setSrc(el,c){el.classList.toggle('collapsed',c);var b=el.querySelector('.mini');"
    "if(b)b.textContent=c?'Show older':'Hide older';}"
    "function toggleSrc(b){var el=b.closest('.src');setSrc(el,!el.classList.contains('collapsed'));}"
    "function toggleOlder(){var t=document.getElementById('tg');var c=t.textContent.indexOf('Hide')===0;"
    "document.querySelectorAll('.src').forEach(function(el){setSrc(el,c);});"
    "t.textContent=c?'Show all stories':'Hide older stories';}"
    "</script>"
)


def render(grouped, errors, tz, now, heading, page_links=None, new_links=None, since_label=None,
           toggle=False):
    """toggle=True builds the web page (buttons, contents, back-to-top); False builds the email."""
    esc = html.escape
    nt = " target='_blank' rel='noopener noreferrer'"
    total = sum(len(items) for groups in grouped.values() for _, items, _ in groups)
    # The web page uses (almost) the full width of the screen, with a small side margin that
    # grows a little on bigger screens. The email keeps a 680px column, which email apps expect.
    box = ("padding:20px max(12px,3vw) 72px" if toggle
           else "max-width:680px;margin:auto;padding:20px 16px 72px")
    p = ["<!DOCTYPE html><html><head><meta charset='utf-8'>",
         "<meta name='viewport' content='width=device-width,initial-scale=1'>",
         f"<title>{esc(heading)}</title>",
         f"<style>html{{scroll-behavior:smooth}}{PAGE_CSS}</style></head>",
         "<body style='margin:0;background:#f4f2ee;font-family:-apple-system,Segoe UI,Helvetica,Arial,sans-serif;color:#1d1d1f'>",
         f"<div id='top' style='{box}'>",
         f"<h1 style='font-family:Georgia,serif;font-size:24px;margin:0 0 4px'>{esc(heading)}</h1>",
         f"<p style='color:#777;font-size:13px;margin:0 0 12px'>{total} items · "
         f"updated {now.astimezone(tz):%A %d %B, %H:%M}</p>"]
    if page_links:
        btn = ("display:inline-block;padding:9px 14px;margin:0 8px 8px 0;border-radius:8px;"
               "background:#1d1d1f;color:#fff;text-decoration:none;font-size:14px")
        p.append("<p style='margin:0 0 8px'>" + "".join(
            f"<a href='{esc(url)}'{nt} style='{btn}'>{esc(label)}</a>" for label, url in page_links) + "</p>")
    if toggle:
        p.append("<p style='margin:0 0 8px'><button id='tg' onclick='toggleOlder()' style='padding:8px 14px;"
                 "border-radius:8px;border:1px solid #1d1d1f;background:transparent;color:#1d1d1f;"
                 "font-size:14px;cursor:pointer'>Hide older stories</button></p>")
    if new_links is not None:
        p.append("<p style='color:#777;font-size:12px;margin:0 0 8px'>"
                 "<span style='background:#d93025;color:#fff;font-size:10px;font-weight:600;"
                 f"padding:1px 5px;border-radius:3px'>NEW</span> = published since your last email "
                 f"({esc(since_label or '')})</p>")
    jumps = [f"<a href='#{k}' style='color:#555'>{icon} {label}</a>"
             for k, icon, label in KINDS if grouped.get(k)]
    if jumps:
        p.append("<p style='font-size:14px;margin:0 0 8px'>" + " &nbsp;·&nbsp; ".join(jumps) + "</p>")
    if toggle and grouped.get("written"):   # contents: one chip per written source
        p.append("<div class='toc'>" + "".join(
            f"<a href='#written-{n}'>{esc(name)}</a>"
            for n, (name, _, _) in enumerate(grouped["written"])) + "</div>")

    for kind, icon, label in KINDS:
        groups = grouped.get(kind)
        if not groups:
            continue
        p.append(f"<h2 id='{kind}' style='font-family:Georgia,serif;font-size:20px;"
                 f"margin:28px 0 4px;border-bottom:2px solid #1d1d1f;padding-bottom:4px'>{icon} {label}</h2>")
        for n, (name, items, visible) in enumerate(groups):
            extra = items[visible:]
            mini = ""
            if toggle and kind == "written" and extra:
                mini = "<button class='mini' onclick='toggleSrc(this)'>Hide older</button>"
            p.append(f"<div class='src' id='{kind}-{n}'><div class='srchead'>"
                     f"<h3 style='font-family:Georgia,serif;font-size:19px;font-weight:700;"
                     f"color:#8a5a00;margin:0'>{esc(name)}</h3>{mini}</div>")
            for i in items[:visible]:
                p.append(render_item(i, tz, now, new_links))
            if extra:   # page only: shown by default, hidden by the buttons
                p.append("<div class='older'>")
                for i in extra:
                    p.append(render_item(i, tz, now, new_links))
                p.append(f"</div><p class='older-note'>{len(extra)} older hidden</p>")
            p.append("</div>")
        if toggle:
            p.append("<a class='totop' href='#top'>↑ Back to top</a>")

    if total == 0:
        p.append("<p style='color:#777'>Nothing new from your sources since last time.</p>")
    if errors:
        p.append("<p style='color:#a33;font-size:12px;margin-top:28px'>Couldn't reach: "
                 + esc("; ".join(errors)) + "</p>")
    p.append("</div>")
    if toggle:
        p.append("<a id='fab' href='#top' aria-label='Back to top'>↑</a>" + PAGE_JS)
    p.append("</body></html>")
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
    recipients = [a.strip() for a in (os.environ.get("DIGEST_TO") or user).split(",") if a.strip()]
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as s:
        s.login(user, pwd)
        for to in recipients:          # one email each, so nobody sees the other addresses
            msg = MIMEMultipart("alternative")
            msg["Subject"], msg["From"], msg["To"] = subject, user, to
            msg.attach(MIMEText(body, "html", "utf-8"))
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

    email_sources = [s for s in sources if s.get("email", True) is not False]
    collected, errors = collect(settings, email_sources, now)
    grouped = select(collected, settings, for_email=True, skip=set(state["seen"]))
    heading = f"Your news · {now_local:%H:%M}"
    body, total = render(grouped, errors, tz, now, heading)
    send_email(f"{heading} · {total} new", body)

    for groups in grouped.values():
        for _, items, _ in groups:
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
    grouped = select(collected, settings, for_email=False)
    links = None
    repo = os.environ.get("GITHUB_REPOSITORY")
    if with_buttons and repo:
        base = f"https://github.com/{repo}/actions/workflows"
        links = [("↻ Refresh this page", f"{base}/page.yml"),
                 ("✉ Email me now", f"{base}/email.yml")]
    # NEW = published since the last email went out
    new_links, since_label = None, None
    if state.get("last_sent"):
        last = datetime.fromisoformat(state["last_sent"])
        new_links = {i["link"] for g in grouped.values() for _, its, _ in g for i in its
                     if i["time"] and i["time"] > last}
        since_label = fmt_time(last, tz, now)
    body, total = render(grouped, errors, tz, now, "Your news",
                         page_links=links, new_links=new_links, since_label=since_label, toggle=True)
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
