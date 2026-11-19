"""Daily news agent.

Reads the sources in config.toml, picks the most important new stories,
checks every claim against the article it came from, and writes a short
brief to briefs/YYYY-MM-DD.md. See README.md for how to use it.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import sys
import tomllib
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

import anthropic
import feedparser
import requests
from bs4 import BeautifulSoup
from pydantic import BaseModel

MODEL = "claude-opus-5-5"
# If the model declines a request, the API retries it on a suitable fallback model.
FALLBACK_BETA = "server-side-fallback-2026-07-01"
BROWSER_HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; daily-news-brief/1.0)",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}
MIN_ARTICLE_CHARS = 800  # Less text than this usually means a paywall or block page.
MAX_ARTICLE_CHARS = 60_000

client = anthropic.Anthropic()


def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


# --------------------------------------------------------------------------
# Data shapes
# --------------------------------------------------------------------------


@dataclass
class Candidate:
    id: int
    source: str
    title: str
    url: str
    published: dt.datetime | None
    summary: str = ""


@dataclass
class Item:
    headline: str
    why: str
    url: str
    source: str


@dataclass
class RunStats:
    sources_total: int = 0
    sources_read: int = 0
    articles_opened: int = 0
    dropped: list[tuple[str, str]] = field(default_factory=list)  # (title, reason)
    source_errors: list[tuple[str, str]] = field(default_factory=list)


class ListedArticle(BaseModel):
    url: str
    title: str
    published_iso: str  # "" if the page does not show a time
    summary: str


class ListedArticles(BaseModel):
    articles: list[ListedArticle]


class Pick(BaseModel):
    id: int
    reason: str


class Shortlist(BaseModel):
    picks: list[Pick]


class Claim(BaseModel):
    statement: str
    quote: str


class WriteUp(BaseModel):
    usable: bool
    reason_if_not_usable: str
    headline: str
    why_it_matters: str
    claims: list[Claim]


class CheckResult(BaseModel):
    all_supported: bool
    problems: list[str]


# --------------------------------------------------------------------------
# Talking to Claude
# --------------------------------------------------------------------------


def ask(prompt: str, schema: type[BaseModel], system: str, effort: str = "medium"):
    """One structured request to Claude. Returns a parsed object, or None on refusal."""
    response = client.beta.messages.parse(
        model=MODEL,
        max_tokens=16000,
        system=system,
        messages=[{"role": "user", "content": prompt}],
        output_format=schema,
        output_config={"effort": effort},
        betas=[FALLBACK_BETA],
        fallbacks="default",
    )
    if response.stop_reason == "refusal":
        log(f"  model declined the request ({getattr(response.stop_details, 'category', None)})")
        return None
    return response.parsed_output


def run_with_web_fetch(prompt: str, tools: list[dict], max_rounds: int = 4):
    """Run a request that uses Claude's web_fetch tool, continuing paused turns.

    Returns the list of all content blocks Claude produced.
    """
    messages: list[dict] = [{"role": "user", "content": prompt}]
    blocks = []
    for _ in range(max_rounds):
        response = client.beta.messages.create(
            model=MODEL,
            max_tokens=16000,
            messages=messages,
            tools=tools,
            output_config={"effort": "low"},
            betas=[FALLBACK_BETA],
            fallbacks="default",
        )
        blocks.extend(response.content)
        if response.stop_reason != "pause_turn":
            break
        messages.append({"role": "assistant", "content": response.content})
    return blocks


# --------------------------------------------------------------------------
# Step 1: gather candidate stories from each source
# --------------------------------------------------------------------------


def parse_time(value) -> dt.datetime | None:
    if not value:
        return None
    try:
        if isinstance(value, str):
            parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
        else:  # time.struct_time from feedparser
            parsed = dt.datetime(*value[:6], tzinfo=dt.timezone.utc)
    except (ValueError, TypeError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.astimezone(dt.timezone.utc)


def clean_html(text: str) -> str:
    return " ".join(BeautifulSoup(text or "", "html.parser").get_text(" ").split())


def read_rss(source: dict) -> list[dict]:
    resp = requests.get(source["url"], headers=BROWSER_HEADERS, timeout=30)
    resp.raise_for_status()
    feed = feedparser.parse(resp.content)
    if feed.bozo and not feed.entries:
        raise ValueError(f"could not read feed: {feed.bozo_exception}")
    out = []
    for e in feed.entries:
        out.append(
            {
                "title": clean_html(e.get("title", "")),
                "url": e.get("link", ""),
                "published": parse_time(e.get("published_parsed") or e.get("updated_parsed")),
                "summary": clean_html(e.get("summary", ""))[:500],
            }
        )
    return out


def read_page(source: dict, now: dt.datetime) -> list[dict]:
    """Have Claude open a section page and list the articles on it."""
    host = urlparse(source["url"]).hostname
    prompt = (
        f"Use web_fetch to open this page: {source['url']}\n\n"
        "Then list every news article linked from it. For each one give the full "
        "article URL exactly as it appears on the page, the headline, the publication "
        "time as an ISO 8601 timestamp in UTC if the page shows one (convert relative "
        f"times such as '3 hours ago' using the current time {now.isoformat()}; use an "
        "empty string if no time is shown), and the one-line summary if the page has "
        "one (otherwise an empty string). Do not invent anything that is not on the page.\n\n"
        "Reply with only a JSON object of the form "
        '{"articles": [{"url": ..., "title": ..., "published_iso": ..., "summary": ...}]}'
    )
    tools = [
        {
            "type": "web_fetch_20260209",
            "name": "web_fetch",
            "max_uses": 2,
            "allowed_domains": [host],
        }
    ]
    blocks = run_with_web_fetch(prompt, tools)
    text = "".join(b.text for b in blocks if b.type == "text")
    match = re.search(r"\{.*\}", text, re.S)
    if not match:
        raise ValueError("could not read the page (no article list came back)")
    listed = ListedArticles.model_validate_json(match.group(0))
    out = []
    for a in listed.articles:
        if urlparse(a.url).hostname is None:
            continue
        out.append(
            {
                "title": a.title,
                "url": a.url,
                "published": parse_time(a.published_iso),
                "summary": a.summary,
            }
        )
    return out


def gather(config: dict, now: dt.datetime, stats: RunStats) -> list[Candidate]:
    since = now - dt.timedelta(hours=config["hours"])
    candidates: list[Candidate] = []
    seen_urls = set()
    for source in config["sources"]:
        stats.sources_total += 1
        log(f"Reading {source['name']} ...")
        try:
            raw = read_rss(source) if source["kind"] == "rss" else read_page(source, now)
        except Exception as exc:  # one bad source should not stop the run
            log(f"  could not read {source['name']}: {exc}")
            stats.source_errors.append((source["name"], str(exc)))
            continue
        stats.sources_read += 1
        kept = 0
        for r in raw:
            url = r["url"].split("#")[0]
            if not url or url in seen_urls:
                continue
            # Keep items inside the time window. Items with no time shown get a
            # chance here; their date is checked again when the article is opened.
            if r["published"] is not None and not (since <= r["published"] <= now + dt.timedelta(hours=1)):
                continue
            seen_urls.add(url)
            candidates.append(
                Candidate(len(candidates), source["name"], r["title"], url, r["published"], r["summary"])
            )
            kept += 1
        log(f"  {kept} new items")
    return candidates


# --------------------------------------------------------------------------
# Step 2: pick the most important stories
# --------------------------------------------------------------------------


def system_prompt(config: dict) -> str:
    ignore = "\n".join(f"- {x}" for x in config["ignore"])
    return (
        f"You are a careful news editor. The brief covers {config['topic']}. "
        f"The reader is {config['reader']}.\n\nLeave out:\n{ignore}"
    )


def shortlist(config: dict, candidates: list[Candidate]) -> list[Candidate]:
    lines = []
    for c in candidates:
        when = c.published.isoformat() if c.published else "unknown"
        lines.append(
            json.dumps(
                {"id": c.id, "source": c.source, "published": when, "title": c.title, "summary": c.summary}
            )
        )
    prompt = (
        f"Here are {len(candidates)} new items from the last {config['hours']} hours, one per line:\n\n"
        + "\n".join(lines)
        + f"\n\nPick up to {config['shortlist']} items that matter most to the reader, most "
        "important first. Skip anything on the leave-out list and anything off topic. When "
        "several items cover the same story, pick only one: prefer the wire service "
        "(Reuters or AP) or the outlet that reported it first. Give each pick a short reason."
    )
    result = ask(prompt, Shortlist, system_prompt(config), effort="high")
    if result is None:
        return []
    by_id = {c.id: c for c in candidates}
    picks, used = [], set()
    for p in result.picks:
        if p.id in by_id and p.id not in used:
            used.add(p.id)
            picks.append(by_id[p.id])
    return picks[: config["shortlist"]]


# --------------------------------------------------------------------------
# Step 3: open each article and get its text
# --------------------------------------------------------------------------


def extract_text(html: str) -> tuple[str, dt.datetime | None]:
    soup = BeautifulSoup(html, "html.parser")
    published = None
    for attrs in (
        {"property": "article:published_time"},
        {"name": "article:published_time"},
        {"itemprop": "datePublished"},
        {"name": "date"},
        {"name": "parsely-pub-date"},
    ):
        tag = soup.find("meta", attrs=attrs)
        if tag and tag.get("content"):
            published = parse_time(tag["content"])
            if published:
                break
    if published is None:
        t = soup.find("time", attrs={"datetime": True})
        if t:
            published = parse_time(t["datetime"])
    for bad in soup(["script", "style", "nav", "footer", "header", "aside", "form", "noscript"]):
        bad.decompose()
    root = soup.find("article") or soup.body or soup
    paragraphs = [" ".join(p.get_text(" ").split()) for p in root.find_all(["p", "h1", "h2", "li"])]
    text = "\n".join(p for p in paragraphs if p)
    return text, published


def fetch_with_claude(url: str) -> str:
    """Fallback when a site blocks plain downloads: fetch the page through Claude's
    web_fetch tool and take the page text from the tool result itself (not from
    anything Claude writes), so the checks below still compare against the real page.
    """
    tools = [
        # The basic version returns the page text unchanged, which is what we need.
        {"type": "web_fetch_20250910", "name": "web_fetch", "max_uses": 1,
         "allowed_domains": [urlparse(url).hostname]},
    ]
    prompt = f"Use web_fetch to open {url} . Then reply with the single word DONE."
    for block in run_with_web_fetch(prompt, tools, max_rounds=2):
        if block.type == "web_fetch_tool_result" and getattr(block.content, "type", "") == "web_fetch_result":
            source = block.content.content.source
            if source.type == "text":
                return source.data
    return ""


def open_article(c: Candidate) -> tuple[str, dt.datetime | None]:
    text, published = "", None
    try:
        resp = requests.get(c.url, headers=BROWSER_HEADERS, timeout=30)
        if resp.ok and "html" in resp.headers.get("content-type", ""):
            text, published = extract_text(resp.text)
    except requests.RequestException as exc:
        log(f"  direct download failed: {exc}")
    if len(text) < MIN_ARTICLE_CHARS:
        log("  direct download gave too little text; trying web_fetch")
        try:
            text = fetch_with_claude(c.url) or text
        except anthropic.APIError as exc:
            log(f"  web_fetch failed: {exc}")
    return text[:MAX_ARTICLE_CHARS], published


# --------------------------------------------------------------------------
# Step 4: write each item and check it against the article
# --------------------------------------------------------------------------


def normalise(s: str) -> str:
    s = unicodedata.normalize("NFKC", s)
    s = s.translate(str.maketrans({"‘": "'", "’": "'", "“": '"', "”": '"',
                                   "–": "-", "—": "-", " ": " "}))
    return " ".join(s.lower().split())


def quotes_found(claims: list[Claim], article: str) -> list[str]:
    """Return the quotes that do NOT appear word for word in the article."""
    body = normalise(article)
    missing = []
    for claim in claims:
        q = normalise(claim.quote).strip(" .\"'")
        if len(q) < 15 or q not in body:
            missing.append(claim.quote)
    return missing


def write_and_check(config: dict, c: Candidate, article: str) -> tuple[Item | None, str]:
    system = system_prompt(config)
    write_prompt = (
        f"Article from {c.source} ({c.url}):\n<article>\n{article}\n</article>\n\n"
        "Write one brief item about this article.\n"
        "- headline: one line, plain and factual.\n"
        "- why_it_matters: exactly two sentences on why this matters to the reader.\n"
        "- claims: every fact used in the headline and why_it_matters (names, numbers, "
        "dates, what happened). For each, copy a short supporting quote word for word "
        "from the article text above (at least a few words, no changes, no ellipses).\n"
        "Use only facts the article states. Set usable to false, with a reason, if the "
        "article is on the leave-out list, is off topic, or does not contain enough text "
        "to support an item (for example a paywall or error page)."
    )
    w = ask(write_prompt, WriteUp, system)
    if w is None:
        return None, "the model declined to write it"
    if not w.usable:
        return None, f"not usable: {w.reason_if_not_usable}"
    if not w.claims:
        return None, "no checkable claims"
    missing = quotes_found(w.claims, article)
    if missing:
        return None, f"quote not found in the article: {missing[0][:80]!r}"

    check_prompt = (
        f"<article>\n{article}\n</article>\n\n"
        f"<item>\nHeadline: {w.headline}\nWhy it matters: {w.why_it_matters}\n</item>\n\n"
        "Check the item against the article. Is every factual statement in it (names, "
        "numbers, dates, events, who said what) directly supported by the article? "
        "Advice or interpretation about why it matters to the reader is fine as long as "
        "it does not state new facts. Set all_supported to false and list the problems "
        "if anything is unsupported, overstated, or wrong."
    )
    check = ask(check_prompt, CheckResult, "You are a strict fact-checker.", effort="high")
    if check is None:
        return None, "the model declined to check it"
    if not check.all_supported:
        return None, "fact check failed: " + "; ".join(check.problems)[:200]
    return Item(w.headline.strip(), w.why_it_matters.strip(), c.url, c.source), ""


# --------------------------------------------------------------------------
# Putting it together
# --------------------------------------------------------------------------


def has_link(url: str) -> bool:
    """True if the URL is a full web address (http or https, with a site name)."""
    parsed = urlparse(url or "")
    return parsed.scheme in ("http", "https") and bool(parsed.netloc)


def build_items(config: dict, picks: list[Candidate], now: dt.datetime, stats: RunStats) -> list[Item]:
    since = now - dt.timedelta(hours=config["hours"])
    items: list[Item] = []
    for c in picks:
        if len(items) >= config["items"]:
            break
        log(f"Checking: {c.title}")
        if not has_link(c.url):
            stats.dropped.append((c.title, f"no link (got {c.url!r})"))
            log("  dropped: no link")
            continue
        article, published = open_article(c)
        stats.articles_opened += 1
        when = published or c.published
        if when is None:
            reason = "could not confirm when it was published"
        elif when < since:
            reason = f"older than {config['hours']} hours (published {when:%Y-%m-%d %H:%M} UTC)"
        elif len(article) < MIN_ARTICLE_CHARS:
            reason = "could not open the article to check it"
        else:
            item, reason = write_and_check(config, c, article)
            if item:
                items.append(item)
                log("  kept")
                continue
        stats.dropped.append((c.title, reason))
        log(f"  dropped: {reason}")
    return items


def render(config: dict, items: list[Item], stats: RunStats, now: dt.datetime) -> str:
    lines = [f"# News brief, {now.day} {now:%B %Y}", "", f"For {config['reader']}.", ""]
    if not items:
        lines += ["No items passed the checks today.", ""]
    for n, it in enumerate(items, 1):
        lines += [f"{n}. **{it.headline}**  ", f"   {it.why}  ", f"   [{it.source}]({it.url})", ""]
    lines.append(
        f"Read {stats.sources_read} of {stats.sources_total} sources and opened "
        f"{stats.articles_opened} articles; dropped {len(stats.dropped)} items I could not check."
    )
    return "\n".join(lines) + "\n"


def render_notes(stats: RunStats) -> str:
    lines = ["# Run notes", ""]
    if stats.source_errors:
        lines += ["## Sources that could not be read", ""]
        lines += [f"- {name}: {err}" for name, err in stats.source_errors] + [""]
    lines += ["## Dropped items", ""]
    lines += [f"- {title}: {reason}" for title, reason in stats.dropped] or ["- none"]
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description="Write today's news brief.")
    parser.add_argument("--config", default="config.toml")
    parser.add_argument("--out", default="briefs")
    args = parser.parse_args()

    config = tomllib.loads(Path(args.config).read_text())
    now = dt.datetime.now(dt.timezone.utc)
    stats = RunStats()

    candidates = gather(config, now, stats)
    log(f"{len(candidates)} new items in total")
    picks = shortlist(config, candidates) if candidates else []
    log(f"{len(picks)} on the shortlist")
    items = build_items(config, picks, now, stats)

    # Last check before saving: every item in the brief must have a link.
    unlinked = [it.headline for it in items if not has_link(it.url)]
    if unlinked:
        log(f"ERROR: {len(unlinked)} item(s) have no link; not saving the brief: {unlinked}")
        return 2

    brief = render(config, items, stats, now)
    out = Path(args.out)
    out.mkdir(exist_ok=True)
    (out / f"{now:%Y-%m-%d}.md").write_text(brief)
    (out / f"{now:%Y-%m-%d}.notes.md").write_text(render_notes(stats))
    print(brief)
    return 0 if stats.sources_read else 1


if __name__ == "__main__":
    sys.exit(main())
