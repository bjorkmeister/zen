#!/usr/bin/env python3
"""Tyyni: a calm one-page broadsheet for Espoo, in more than one edition.

Each reader has a profile: their sources, editorial voice, edition hours and
how they hear that a new edition is out. For a profile, this script
  1. reads the weather and daylight for Olari, Espoo from Open-Meteo,
  2. reads recent items from the profile's curated RSS feeds (and, for some,
     Helsinki and Espoo cultural events from the Linked Events API),
  3. asks Gemini to distil them into one calm page in Finnish, and
  4. renders template.html into the profile's index.html.

There is no state: every run starts from nothing and overwrites the page.
If any required step fails the script exits non-zero and writes nothing, so
the previous edition stays up rather than a broken one going out.

    python generate.py                  write the main edition
    python generate.py --profile minna  write Minna's edition
    python generate.py --due            list the profiles whose latest edition is missing
    python generate.py --send minna     email Minna the edition just written

`--due` needs nothing beyond the standard library, which is why the
third-party imports live inside the functions that use them.

Requires GEMINI_API_KEY; sending email also needs GMAIL_USER and
GMAIL_APP_PASSWORD. Dependencies: google-genai, feedparser.
"""

from __future__ import annotations

import argparse
import calendar
import html
import json
import os
import re
import smtplib
import sys
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from email.message import EmailMessage
from email.utils import formataddr
from pathlib import Path
from typing import Callable
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent
TEMPLATE = ROOT / "template.html"
EMAIL_TEMPLATE = ROOT / "email.html"

TZ = ZoneInfo("Europe/Helsinki")
OLARI = {"latitude": 60.1772, "longitude": 24.7383}  # Olari, Espoo

LEAD = timedelta(minutes=20)  # a run this close before an edition hour belongs to it

# Tried in order. Carbon writes best but needs a paid key; Flash and Hydrogen
# are on the free tier, so the paper still appears if Carbon is unavailable.
MODELS = ("gemini-4-carbon", "gemini-3.8-flash", "gemini-4-hydrogen")
USER_AGENT = "Mozilla/5.0 (compatible; Tyyni/1.0; personal daily digest)"
ITEMS_PER_FEED = 10
SUMMARY_CHARS = 320


# --------------------------------------------------------------------------
# Sources
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class Feed:
    name: str
    url: str


@dataclass(frozen=True)
class Section:
    key: str
    label: str
    feeds: tuple[Feed, ...] = ()
    events: bool = False  # Helsinki and Espoo cultural events instead of feeds


# --------------------------------------------------------------------------
# Profiles
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class Palette:
    paper: str
    ink: str
    stone: str
    rule: str


COTTON = Palette(paper="#F6F4EF", ink="#181818", stone="#76726A", rule="#E2DFD7")   # warm cotton paper
ROSA = Palette(paper="#F7EEEA", ink="#1B1616", stone="#7C6D69", rule="#EADCD6")     # the same, with a blush of rose


@dataclass(frozen=True)
class Profile:
    key: str
    folder: str                      # where the page lives under the site root
    editions: dict[int, str]         # Helsinki hour -> edition name; the first one notifies
    notify: str                      # "github": a comment in the issue thread; "email"
    sections: tuple[Section, ...]
    window_hours: int                # how far back the feeds are read
    min_sections: int                # fewer answering sections than this: keep the old page
    reader: str                      # who reads it and what the page is for
    principles: str                  # what to choose and what to leave out
    synthesis_label: str
    synthesis: str
    signal_why: str                  # what the second sentence of each signal explains
    signal_notes: str
    reflection_label: str
    reflection: str
    email_to_env: str = ""           # secret holding the recipient; empty sends to GMAIL_USER
    palette: Palette = COTTON

    @property
    def output(self) -> Path:
        return ROOT / self.folder / "index.html"

    @property
    def mail_data(self) -> Path:
        return ROOT / f".edition-{self.key}.json"  # handed from writing to sending; never committed


MAIN = Profile(
    key="main",
    folder="",
    editions={6: "aamupainos", 14: "iltapäiväpainos", 20: "iltapainos"},
    notify="github",
    window_hours=36,  # "yesterday", with slack for time zones and late publishing
    min_sections=4,
    sections=(
        Section("tekniikka", "Tekniikka", (
            Feed("Ars Technica", "https://feeds.arstechnica.com/arstechnica/index"),
            Feed("Hacker News", "https://hnrss.org/frontpage?points=200"),
        )),
        Section("talous", "Talous", (
            Feed("Financial Times", "https://www.ft.com/rss/home"),
            Feed("The Economist", "https://www.economist.com/finance-and-economics/rss.xml"),
            Feed("Bloomberg", "https://feeds.bloomberg.com/economics/news.rss"),
        )),
        Section("design", "Design", (
            Feed("Dezeen", "https://www.dezeen.com/feed/"),
            Feed("Wallpaper*", "https://www.wallpaper.com/rss"),
        )),
        Section("media", "Media", (
            Feed("Nieman Lab", "https://www.niemanlab.org/feed/"),
            Feed("The Guardian", "https://www.theguardian.com/media/rss"),
            Feed("The New Yorker", "https://www.newyorker.com/feed/culture"),
        )),
        Section("huippu_urheilu", "Huippu-urheilu", (
            Feed("BBC Sport", "https://feeds.bbci.co.uk/sport/rss.xml"),
            Feed("The Athletic", "https://www.nytimes.com/athletic/rss/news/"),
        )),
    ),
    reader=(
        "You are the sole editor of Tyyni (\"still, calm\"), a one-page broadsheet with exactly one reader: "
        "a senior Finnish leader in Espoo who works where technology, organisations and strategy meet. "
        "The sheet appears three times a day – morning (6.00), afternoon (14.00) and evening (20.00) – and the "
        "reader reads each edition once, slowly, and then closes the screen. The briefing says which edition "
        "this is; let the atmosphere and the zen anchor fit that hour of the day. Your task is to give them "
        "clarity and calm: the few things that matter, why they matter, and nothing else."
    ),
    principles=(
        "- Signal over noise. Choose events with structural consequence: shifts in incentives, capital, "
        "regulation, platforms, institutions or power. Pass over product launches, minor deals, rumours, "
        "previews, viewing guides, listicles, celebrity, and opinion pieces unless they reveal something structural."
    ),
    synthesis_label="Pohjavirrat",
    synthesis=(
        "one paragraph of 3–4 sentences. Look beneath the individual headlines and name the structural current "
        "that links at least two of the domains. Name the mechanism – who gains leverage, which constraint moves, "
        "which incentive changes – rather than the topic. Do not summarise the headlines one by one."
    ),
    signal_why="why it matters structurally",
    signal_notes="",
    reflection_label="Pohdinta",
    reflection=(
        "one paragraph of 3–5 sentences for a senior leader. Start from a concrete pattern visible in today's "
        "material and draw a first-principles insight about organisations, strategy or systems – for example "
        "incentive structures, second-order effects, governance friction, Conway's law, information asymmetry, "
        "principal–agent problems, operational leverage, feedback delays or optionality. It must be specific "
        "enough that it could not have been written on any other day. Forbidden: management clichés and "
        "motivational language (such as \"muutos on ainoa pysyvä\", \"kulttuuri syö strategian aamiaiseksi\", "
        "ketteryys, synergia, paradigman muutos, mukavuusalue, win-win), lists, advice in the imperative, and questions."
    ),
)

MINNA = Profile(
    key="minna",
    folder="minna",
    editions={6: "aamupainos"},
    notify="email",
    email_to_env="MINNA_EMAIL",
    palette=ROSA,
    window_hours=72,  # design, food and culture do not go stale overnight
    min_sections=5,
    sections=(
        Section("design", "Design", (
            Feed("Dezeen", "https://www.dezeen.com/feed/"),
            Feed("Wallpaper*", "https://www.wallpaper.com/rss"),
            Feed("Design Milk", "https://design-milk.com/feed/"),
        )),
        Section("lifestyle", "Lifestyle", (
            Feed("Monocle", "https://monocle.com/feed/"),
            Feed("The Guardian", "https://www.theguardian.com/lifeandstyle/rss"),
            Feed("Condé Nast Traveler", "https://www.cntraveler.com/feed/rss"),
        )),
        Section("hyvinvointi", "Hyvinvointi", (
            Feed("Psyche", "https://psyche.co/feed"),
            Feed("Greater Good", "https://greatergood.berkeley.edu/site/rss/articles"),
            Feed("The Guardian", "https://www.theguardian.com/lifeandstyle/health-and-wellbeing/rss"),
        )),
        Section("ravintolat", "Ravintolat", (
            Feed("Helsingin Sanomat", "https://www.hs.fi/rss/ruoka.xml"),
            Feed("Eater", "https://www.eater.com/rss/index.xml"),
            Feed("The Guardian", "https://www.theguardian.com/food/rss"),
        )),
        Section("trendit", "Trendit", (
            Feed("Vogue", "https://www.vogue.com/feed/rss"),
            Feed("The Guardian", "https://www.theguardian.com/fashion/rss"),
            Feed("Harper's Bazaar", "https://www.harpersbazaar.com/rss/all.xml/"),
        )),
        Section("kulttuuri", "Kulttuuri", (
            Feed("Helsingin Sanomat", "https://www.hs.fi/rss/kulttuuri.xml"),
            Feed("The Guardian", "https://www.theguardian.com/culture/rss"),
        )),
        Section("tapahtumat", "Tapahtumat", events=True),
    ),
    reader=(
        "You are the sole editor of Tyyni (\"still, calm\"), a one-page sheet with exactly one reader: a woman in "
        "Olari, Espoo, with a refined eye for design, interiors, food, wellbeing and culture. The sheet appears "
        "once a day, at 6.00; she reads it once, slowly, with her first coffee, and then closes the screen. Your "
        "task is to bring her beauty, inspiration and calm: a handful of things worth her attention today, and why."
    ),
    principles=(
        "- Beauty and substance over noise. Choose what is genuinely well made, thoughtful, new or useful: "
        "architecture, interiors and objects with craft; restaurants and food made with care; wellbeing grounded "
        "in research; culture worth leaving the house for; trends that say something about how people want to live.\n"
        "- Leave out entirely: war, politics, economy, markets, technology business, crime, disasters and health "
        "scares. Leave out too: celebrity gossip, red carpets, shopping lists and gift guides, sales pressure, "
        "diet culture, body ideals and influencer hype.\n"
        "- Prefer what is near: Helsinki and Espoo first when the material allows, then the Nordic countries, then the world.\n"
        "- Write to her as an equal: no clichés about women, no \"hemmottele itseäsi\", no self-care jargon."
    ),
    synthesis_label="Virtaukset",
    synthesis=(
        "one paragraph of 3–4 sentences. Name the current that runs through today's material – a shared mood, "
        "material, colour, value or way of living that shows in at least two domains. Say precisely what it is "
        "and where it shows; do not list the items one by one."
    ),
    signal_why="why it is worth her attention – what makes it beautiful, interesting or useful in her own life",
    signal_notes=(
        "For ravintolat, prefer restaurant and food news from Helsinki or Espoo when there is any. For tapahtumat, "
        "choose the one event in Helsinki or Espoo that a person with her taste would most enjoy, and name the "
        "venue and the day and time in the first sentence."
    ),
    reflection_label="Ajatus",
    reflection=(
        "one paragraph of 3–4 sentences: a quiet, grounded thought about living well, drawn from something "
        "concrete in today's material – attention, rhythm, materials and craft, rest, seasons, friendship, the "
        "home. It must be specific enough that it could not have been written on any other day. Forbidden: "
        "self-help clichés (such as \"pienet asiat\", \"hetkessä eläminen\", \"oma aika\", \"self care\"), "
        "advice in the imperative, and questions."
    ),
)

PROFILES = {profile.key: profile for profile in (MAIN, MINNA)}


# --------------------------------------------------------------------------
# Fetching
# --------------------------------------------------------------------------

def fetch(url: str, attempts: int = 3, timeout: float = 20.0) -> bytes:
    """GET a URL, retrying transient failures with a short backoff."""
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    for attempt in range(1, attempts + 1):
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return response.read()
        except Exception:
            if attempt == attempts:
                raise
            time.sleep(2 ** attempt)
    raise AssertionError("unreachable")


@dataclass(frozen=True)
class Weather:
    now: float
    feels_like: float
    high: float
    low: float
    condition: str
    wind_ms: float
    precipitation_mm: float
    precipitation_chance: int | None
    sunrise: datetime
    sunset: datetime
    daylight_s: float
    daylight_change_s: float


# WMO weather interpretation codes, as used by Open-Meteo.
WMO_FI = {
    0: "selkeää", 1: "enimmäkseen selkeää", 2: "puolipilvistä", 3: "pilvistä",
    45: "sumua", 48: "huurtavaa sumua",
    51: "heikkoa tihkusadetta", 53: "tihkusadetta", 55: "tiheää tihkusadetta",
    56: "jäätävää tihkua", 57: "voimakasta jäätävää tihkua",
    61: "heikkoa vesisadetta", 63: "vesisadetta", 65: "voimakasta vesisadetta",
    66: "jäätävää sadetta", 67: "voimakasta jäätävää sadetta",
    71: "heikkoa lumisadetta", 73: "lumisadetta", 75: "voimakasta lumisadetta",
    77: "lumijyväsiä",
    80: "heikkoja sadekuuroja", 81: "sadekuuroja", 82: "rankkoja sadekuuroja",
    85: "lumikuuroja", 86: "voimakkaita lumikuuroja",
    95: "ukkosta", 96: "ukkosta ja raetta", 99: "voimakasta ukkosta ja raetta",
}


def fetch_weather() -> Weather:
    params = {
        **OLARI,
        "timezone": "Europe/Helsinki",
        "wind_speed_unit": "ms",
        "past_days": 1,       # yesterday's daylight, for the change in day length
        "forecast_days": 1,
        "current": "temperature_2m,apparent_temperature,weather_code,wind_speed_10m",
        "daily": ",".join((
            "weather_code", "temperature_2m_max", "temperature_2m_min",
            "sunrise", "sunset", "daylight_duration",
            "precipitation_sum", "precipitation_probability_max", "wind_speed_10m_max",
        )),
    }
    url = "https://api.open-meteo.com/v1/forecast?" + urllib.parse.urlencode(params)
    data = json.loads(fetch(url))
    current, daily = data["current"], data["daily"]
    today = -1  # daily arrays are [yesterday, today]
    return Weather(
        now=current["temperature_2m"],
        feels_like=current["apparent_temperature"],
        high=daily["temperature_2m_max"][today],
        low=daily["temperature_2m_min"][today],
        condition=WMO_FI.get(daily["weather_code"][today], "vaihtelevaa säätä"),
        wind_ms=daily["wind_speed_10m_max"][today],
        precipitation_mm=daily["precipitation_sum"][today] or 0.0,
        precipitation_chance=daily["precipitation_probability_max"][today],
        sunrise=datetime.fromisoformat(daily["sunrise"][today]),
        sunset=datetime.fromisoformat(daily["sunset"][today]),
        daylight_s=daily["daylight_duration"][today],
        daylight_change_s=daily["daylight_duration"][today] - daily["daylight_duration"][0],
    )


@dataclass(frozen=True)
class Item:
    id: str
    section: str
    source: str
    title: str
    summary: str
    when: str      # "5 h" for news, "ti 18.11. klo 19.00" for events


TAG = re.compile(r"<[^>]+>")
URL = re.compile(r"https?://\S+")
SPACE = re.compile(r"\s+")
FEED_METADATA = re.compile(r"(Article URL|Comments URL|Points|# Comments):.*$", re.S)
# Formats that are never signal, whatever the story.
NOISE = re.compile(
    r"\bhow to watch\b|\blive(?: updates| blog)?:|\blive updates\b|\bgossip\b|\bplayer ratings\b"
    r"|\bpodcast\b|\bquiz\b|\bcrossword\b|\bbest deals?\b|\bgift guide\b|\bnewsletter\b",
    re.I,
)


def clean(text: str) -> str:
    return SPACE.sub(" ", html.unescape(TAG.sub(" ", text or ""))).strip()


def shorten(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    cut = text[:limit].rsplit(" ", 1)[0]
    return cut.rstrip(" ,;:.–-") + "…"


def read_feed(feed: Feed, now: float, window_hours: int) -> list[dict]:
    """Recent entries from one feed, newest first. A failing feed yields nothing."""
    try:
        import feedparser
        parsed = feedparser.parse(fetch(feed.url))
    except Exception as error:
        print(f"  ! {feed.name}: {type(error).__name__}: {error}", file=sys.stderr)
        return []
    entries = []
    for entry in parsed.entries:
        stamp = entry.get("published_parsed") or entry.get("updated_parsed")
        if not stamp:
            continue
        age = (now - calendar.timegm(stamp)) / 3600
        if not -1 <= age <= window_hours:
            continue
        title = clean(entry.get("title", ""))
        if not title or NOISE.search(title):
            continue
        summary = clean(FEED_METADATA.sub("", URL.sub(" ", clean(entry.get("summary", "")))))
        if summary.lower().startswith(title.lower()[:40]):
            summary = summary[len(title):].strip(" .:–-")
        entries.append({
            "source": feed.name,
            "title": title,
            "summary": shorten(summary, SUMMARY_CHARS) if len(summary) >= 25 else "",
            "age": max(0, age),
        })
    entries.sort(key=lambda e: e["age"])
    return [{"source": e["source"], "title": e["title"], "summary": e["summary"], "when": f"{round(e['age'])} h"}
            for e in entries[:ITEMS_PER_FEED]]


# Linked Events (api.hel.fi): concerts, exhibitions, theatre, dance and film
# starting in the next few days, without the children's, family, youth and
# senior programmes and the courses and workshops that make up most of the feed.
EVENT_KEYWORDS = "yso:p11185,yso:p5121,yso:p2625,yso:p1278,yso:p1235"
EVENT_AUDIENCES_LEFT_OUT = {
    "yso:p4354", "yso:p13050", "yso:p11617", "yso:p2433",
    "yso:p20513", "yso:p6915", "yso:p16485", "yso:p15937",
}
EVENT_NOT_FOR_HER = re.compile(
    r"lapsi|lasten|lapset|taapero|perhe|vauva|baby|leikki|nuori|nuorten|seniori|koulu|päiväkoti"
    r"|eläkel|palvelukeskus|harjoitu|kurssi|työpaja|kerho",
    re.I,
)
EVENT_DAYS = 3
MAX_EVENTS = 40
WEEKDAYS_SHORT = ("ma", "ti", "ke", "to", "pe", "la", "su")


def read_events(now: datetime) -> list[dict]:
    """Coming cultural events in Helsinki and Espoo. A failing API yields nothing."""
    params = {
        "start": "today",
        "end": (now.date() + timedelta(days=EVENT_DAYS)).isoformat(),
        "division": "espoo,helsinki",
        "keyword": EVENT_KEYWORDS,
        "super_event_type": "none",
        "max_duration": 86400,
        "sort": "start_time",
        "page_size": 100,
        "include": "location",
    }
    try:
        data = json.loads(fetch("https://api.hel.fi/linkedevents/v1/event/?" + urllib.parse.urlencode(params)))
    except Exception as error:
        print(f"  ! Linked Events: {type(error).__name__}: {error}", file=sys.stderr)
        return []
    entries, seen = [], set()
    for event in data.get("data", []):
        def finnish(value) -> str:
            value = value or {}
            return clean(value.get("fi") or value.get("en") or value.get("sv") or "")
        name = finnish(event.get("name"))
        venue = finnish((event.get("location") or {}).get("name"))
        keywords = {k.get("@id", "").rstrip("/").rsplit("/", 1)[-1] for k in event.get("keywords") or []}
        try:
            start = datetime.fromisoformat(event["start_time"].replace("Z", "+00:00")).astimezone(TZ)
        except (KeyError, TypeError, ValueError):
            continue
        if (not name or name in seen or start < now
                or keywords & EVENT_AUDIENCES_LEFT_OUT
                or EVENT_NOT_FOR_HER.search(f"{name} {venue}")
                or (event.get("audience_max_age") or 99) < 18):
            continue
        seen.add(name)
        entries.append({
            "source": venue or "Helsinki",
            "title": name,
            "summary": shorten(finnish(event.get("short_description")), SUMMARY_CHARS),
            "when": f"{WEEKDAYS_SHORT[start.weekday()]} {start.day}.{start.month}. klo {clock(start)}",
        })
    return entries[:MAX_EVENTS]


def fetch_news(profile: Profile) -> dict[str, list[Item]]:
    now_ts, now = time.time(), datetime.now(TZ)
    jobs: list[tuple[Section, Callable[[], list[dict]]]] = []
    for section in profile.sections:
        if section.events:
            jobs.append((section, lambda: read_events(now)))
        for feed in section.feeds:
            jobs.append((section, lambda feed=feed: read_feed(feed, now_ts, profile.window_hours)))
    with ThreadPoolExecutor(max_workers=len(jobs)) as pool:
        results = list(pool.map(lambda job: job[1](), jobs))

    news: dict[str, list[Item]] = {section.key: [] for section in profile.sections}
    seen: set[str] = set()
    for (section, _), entries in zip(jobs, results):
        for entry in entries:
            fingerprint = re.sub(r"\W+", "", entry["title"].lower())
            if fingerprint in seen:
                continue
            seen.add(fingerprint)
            bucket = news[section.key]
            bucket.append(Item(id=f"{section.key[:2].upper()}{len(bucket) + 1}",
                               section=section.key, **entry))
    return news


# --------------------------------------------------------------------------
# Finnish formatting
# --------------------------------------------------------------------------

WEEKDAYS = ("maanantai", "tiistai", "keskiviikko", "torstai", "perjantai", "lauantai", "sunnuntai")
MONTHS = ("tammikuuta", "helmikuuta", "maaliskuuta", "huhtikuuta", "toukokuuta", "kesäkuuta",
          "heinäkuuta", "elokuuta", "syyskuuta", "lokakuuta", "marraskuuta", "joulukuuta")


def degrees(value: float) -> str:
    n = round(value)
    return f"{'−' if n < 0 else ''}{abs(n)}°"


def clock(moment: datetime) -> str:
    return f"{moment.hour}.{moment.minute:02d}"


def duration(seconds: float) -> str:
    minutes = round(seconds / 60)
    return f"{minutes // 60} h {minutes % 60} min"


def daylight_change(seconds: float) -> str:
    minutes = round(seconds / 60)
    if minutes == 0:
        return "sama kuin eilen"
    return f"{'+' if minutes > 0 else '−'}{abs(minutes)} min"


def long_date(day: datetime | date) -> str:
    return f"{WEEKDAYS[day.weekday()].capitalize()} {day.day}. {MONTHS[day.month - 1]} {day.year}"


def short_date(day: datetime | date) -> str:
    return f"{WEEKDAYS[day.weekday()]} {day.day}.{day.month}."


def editions(now: datetime, hours: dict[int, str]) -> tuple[datetime, datetime]:
    """The edition this run belongs to, and the one after it."""
    anchor = now + LEAD
    slots = sorted(
        (anchor + timedelta(days=offset)).replace(hour=hour, minute=0, second=0, microsecond=0)
        for offset in (-1, 0, 1) for hour in hours
    )
    current = max(slot for slot in slots if slot <= anchor)
    upcoming = min(slot for slot in slots if slot > anchor)
    return current, upcoming


def next_edition_text(now: datetime, upcoming: datetime) -> str:
    when = f"{upcoming.hour:02d}.{upcoming.minute:02d}"
    return when if upcoming.date() == now.date() else f"huomenna {when}"


# --------------------------------------------------------------------------
# Composition
# --------------------------------------------------------------------------

LANGUAGE = """\
Language
Write every field in Finnish: natural, precise, contemporary written Finnish of the kind the best Finnish long-form editors produce. Use Finnish syntax, not translated English. Prefer a good Finnish word to an anglicism, an active verb to a stack of nouns, and avoid officialese such as "toimesta", "omata", "suorittaa" used as a filler verb, or "tulee olemaan". Keep proper names in their original form. Use Finnish typography: ”lainausmerkit”, ajatusviiva (–), kellonajat pisteellä (8.06), desimaalipilkku, miinusmerkki (−3°)."""

SHARED_PRINCIPLES = """\
- Use only the material provided for events. Never invent facts, figures, quotes or outcomes. Widely known background may frame an item, but must not add new news. If an item is thin, write less rather than guess.
- Calm and exact. No hype, no alarm, no exclamation marks, no emoji, no markdown, no rhetorical questions."""

ATMOSPHERE = """\
atmosphere: exactly one sentence, at most 30 words. The weather in Espoo as it stands at this edition's hour and the rhythm of daylight, including the current temperature from the data. Evocative but concrete, true to the data; no stock phrases."""

ZEN = """\
zen_anchor: exactly one sentence, at most 20 words. Timeless, quiet and concrete; it may draw on today's season or light in Espoo. It restores perspective and gently turns the reader toward the rest of their real day – or, in an evening edition, toward rest – without mentioning screens, news, reading or this page, and without quotations, attributed sayings or familiar aphorisms."""


def system_prompt(profile: Profile) -> str:
    keys = ", ".join(section.key for section in profile.sections)
    example = f"{profile.sections[0].key[:2].upper()}3"
    signals = (
        f"signals: one entry per domain ({keys}), each about the single most worthwhile item in that domain's material.\n"
        f"- source_id: the id of the item you chose, exactly as given (for example \"{example}\").\n"
        "- title: a short Finnish headline, at most 8 words, sentence case, no full stop.\n"
        f"- body: exactly two sentences. First, what happened, concretely and with the actor named. Second, {profile.signal_why}. "
        "Objective; no praise words.\n"
        "If a domain's material is thin, choose its least trivial item and state plainly why it matters. Never pad."
    )
    if profile.signal_notes:
        signals += "\n" + profile.signal_notes
    return "\n\n".join((
        profile.reader,
        LANGUAGE,
        "Editorial principles\n" + profile.principles + "\n" + SHARED_PRINCIPLES,
        "Fields",
        ATMOSPHERE,
        "synthesis: " + profile.synthesis,
        signals,
        "reflection: " + profile.reflection,
        ZEN,
    ))


def edition_schema(profile: Profile) -> dict:
    signal = {
        "type": "object",
        "properties": {
            "source_id": {"type": "string"},
            "title": {"type": "string"},
            "body": {"type": "string"},
        },
        "required": ["source_id", "title", "body"],
        "additionalProperties": False,
    }
    keys = [section.key for section in profile.sections]
    return {
        "type": "object",
        "properties": {
            "atmosphere": {"type": "string"},
            "synthesis": {"type": "string"},
            "signals": {
                "type": "object",
                "properties": {key: signal for key in keys},
                "required": keys,
                "additionalProperties": False,
            },
            "reflection": {"type": "string"},
            "zen_anchor": {"type": "string"},
        },
        "required": ["atmosphere", "synthesis", "signals", "reflection", "zen_anchor"],
        "additionalProperties": False,
    }


def briefing(profile: Profile, today: datetime, edition_hour: int, weather: Weather,
             news: dict[str, list[Item]]) -> str:
    rain = f"{weather.precipitation_mm:.1f} mm".replace(".", ",")
    if weather.precipitation_chance is not None:
        rain += f", todennäköisyys {weather.precipitation_chance} %"
    lines = [
        f"Päivä: {long_date(today)}, vuoden {today.timetuple().tm_yday}. päivä.",
        f"Painos: {profile.editions[edition_hour]}, luetaan noin kello {edition_hour}.00.",
        "",
        "Sää Olarissa, Espoossa tänään (Open-Meteo):",
        f"- nyt {degrees(weather.now)}, tuntuu kuin {degrees(weather.feels_like)}",
        f"- päivän ylin {degrees(weather.high)}, alin {degrees(weather.low)}; {weather.condition}",
        f"- sade {rain}; tuuli enimmillään {round(weather.wind_ms)} m/s",
        f"- aurinko nousee {clock(weather.sunrise)} ja laskee {clock(weather.sunset)}",
        f"- valoisaa {duration(weather.daylight_s)} ({daylight_change(weather.daylight_change_s)} eiliseen)",
        "",
        f"Aineisto: uutiset viimeiseltä {profile.window_hours} tunnilta, tapahtumat seuraavilta päiviltä. "
        "Muoto: [id] lähde · ikä tai ajankohta – otsikko – tiivistelmä.",
    ]
    for section in profile.sections:
        lines += ["", f"## {section.key} ({section.label})"]
        items = news[section.key]
        if not items:
            lines.append("(ei aineistoa tänään)")
        for item in items:
            text = f"[{item.id}] {item.source} · {item.when} – {item.title}"
            if item.summary:
                text += f" – {item.summary}"
            lines.append(text)
    return "\n".join(lines)


def compose(profile: Profile, prompt: str) -> dict:
    """Ask each model in turn until one returns a complete edition."""
    from google import genai
    from google.genai import errors, types

    # One unhurried attempt per model: deep thinking can take minutes, and a
    # timed-out call is better handed to the next model than repeated.
    client = genai.Client(http_options=types.HttpOptions(
        timeout=600_000,  # milliseconds
        retry_options=types.HttpRetryOptions(attempts=1),
    ))
    config = types.GenerateContentConfig(
        system_instruction=system_prompt(profile),
        response_mime_type="application/json",
        response_json_schema=edition_schema(profile),
        # Thinking counts against max_output_tokens, so leave ample room for both.
        thinking_config=types.ThinkingConfig(thinking_level=types.ThinkingLevel.MEDIUM),
        max_output_tokens=32768,
    )
    for model in MODELS:
        print(f"  asking {model}", flush=True)
        started = time.monotonic()
        try:
            response = client.models.generate_content(model=model, contents=prompt, config=config)
        except (errors.APIError, OSError) as error:  # OSError covers timeouts and dropped connections
            print(f"  ! {model}: {type(error).__name__}: {error}", file=sys.stderr, flush=True)
            continue
        candidate = response.candidates[0] if response.candidates else None
        finish = candidate.finish_reason if candidate else None
        usage = response.usage_metadata
        print(f"  {model}: {time.monotonic() - started:.0f} s, finish {finish}, tokens in "
              f"{usage.prompt_token_count if usage else '?'} / out {usage.candidates_token_count if usage else '?'}"
              f" + thinking {usage.thoughts_token_count if usage else '?'}",
              flush=True)
        if finish != types.FinishReason.STOP or not response.text:
            continue
        try:
            edition = json.loads(response.text)
        except json.JSONDecodeError:
            print(f"  ! {model}: response was not valid JSON", file=sys.stderr)
            continue
        if complete(profile, edition):
            return edition
        print(f"  ! {model}: edition is missing fields", file=sys.stderr)
    raise RuntimeError("no model produced an edition")


def complete(profile: Profile, edition: dict) -> bool:
    text_fields = ("atmosphere", "synthesis", "reflection", "zen_anchor")
    signals = edition.get("signals") or {}
    return (all(str(edition.get(field, "")).strip() for field in text_fields)
            and all(str((signals.get(section.key) or {}).get(part, "")).strip()
                    for section in profile.sections for part in ("title", "body")))


def tidy(text: str) -> str:
    text = SPACE.sub(" ", str(text)).strip()
    if not text:
        raise ValueError("empty field in edition")
    return text


# --------------------------------------------------------------------------
# Rendering
# --------------------------------------------------------------------------

PLACEHOLDER = re.compile(r"\{\{\s*(\w+)\s*\}\}")
SIGNAL_BLOCK = re.compile(r"<!-- signal -->(.*?)<!-- /signal -->", re.S)


def fill(fragment: str, values: dict[str, str]) -> str:
    def substitute(match: re.Match) -> str:
        key = match.group(1)
        if key not in values:
            raise KeyError(f"template placeholder has no value: {key}")
        return html.escape(values[key], quote=True)
    return PLACEHOLDER.sub(substitute, fragment)


def render(template: str, page: dict[str, str], signals: list[dict[str, str]]) -> str:
    block = SIGNAL_BLOCK.search(template)
    if not block:
        raise ValueError("template has no <!-- signal --> block")
    repeated = "".join(fill(block.group(1), {**page, **signal}) for signal in signals)  # rows may use page values too
    return fill(template[:block.start()], page) + repeated + fill(template[block.end():], page)


def build_page(profile: Profile, today: datetime, current: datetime, upcoming: datetime, number: int,
               weather: Weather, news: dict[str, list[Item]],
               edition: dict) -> tuple[dict[str, str], list[dict[str, str]]]:
    sources = {item.id: item.source for items in news.values() for item in items}
    page = {
        **{name: getattr(profile.palette, name) for name in ("paper", "ink", "stone", "rule")},
        "slot": slot_id(current),
        "next_edition": next_edition_text(today, upcoming),
        "date_long": long_date(today),
        "date_short": short_date(today),
        "number": str(number),
        "temp_now": degrees(weather.now),
        "temp_high": degrees(weather.high),
        "sun": f"{clock(weather.sunrise)}–{clock(weather.sunset)}",
        "daylight": duration(weather.daylight_s),
        "daylight_change": daylight_change(weather.daylight_change_s),
        "synthesis_label": profile.synthesis_label,
        "reflection_label": profile.reflection_label,
        "atmosphere": tidy(edition["atmosphere"]),
        "synthesis": tidy(edition["synthesis"]),
        "reflection": tidy(edition["reflection"]),
        "zen_anchor": tidy(edition["zen_anchor"]),
    }
    signals = []
    for section in profile.sections:
        signal = edition["signals"][section.key]
        signals.append({
            "signal_label": section.label,
            "signal_source": sources.get(str(signal.get("source_id", "")).strip(), ""),
            "signal_title": tidy(signal["title"]).rstrip("."),
            "signal_body": tidy(signal["body"]),
        })
    return page, signals


EDITION_NUMBER = re.compile(r'<meta name="edition-number" content="(\d+)">')
EDITION_SLOT = re.compile(r'<meta name="edition-slot" content="([^"]+)">')


def slot_id(slot: datetime) -> str:
    return slot.strftime("%Y-%m-%dT%H:%M")


def current_page(profile: Profile) -> str:
    try:
        return profile.output.read_text(encoding="utf-8")
    except FileNotFoundError:
        return ""


def already_published(profile: Profile, slot: datetime) -> bool:
    """True when the profile's page is already this edition."""
    found = EDITION_SLOT.search(current_page(profile))
    return bool(found) and found.group(1) == slot_id(slot)


def edition_number(profile: Profile) -> int:
    """One more than the edition being replaced. Each page records its own
    number, so the count needs no store of its own and skips no numbers when a
    run fails. A page without a number (or no page) makes this N:o 1."""
    found = EDITION_NUMBER.search(current_page(profile))
    return int(found.group(1)) + 1 if found else 1


def write_outputs(values: dict[str, str]) -> None:
    target = os.environ.get("GITHUB_OUTPUT")
    if target:
        with open(target, "a", encoding="utf-8") as out:
            for key, value in values.items():
                out.write(f"{key}={SPACE.sub(' ', value)}\n")


# --------------------------------------------------------------------------
# Email
# --------------------------------------------------------------------------

def plain_email(page: dict[str, str], signals: list[dict[str, str]], url: str) -> str:
    lines = [f"Tyyni · {page['date_long']} · N:o {page['number']}", "", page["atmosphere"], "",
             page["synthesis"], "", "Tänään lehdessä:"]
    lines += [f"– {signal['signal_label']}: {signal['signal_title']}" for signal in signals]
    lines += ["", url, "", page["zen_anchor"]]
    return "\n".join(lines)


def send_email(profile: Profile) -> int:
    """Email the edition written by the last run of this profile. Without the
    Gmail credentials it says so and does nothing, so setting them up later
    never breaks a run."""
    user, password = os.environ.get("GMAIL_USER", ""), os.environ.get("GMAIL_APP_PASSWORD", "")
    if not (user and password):
        print("  Gmail credentials are not set; no email sent")
        return 0
    data = json.loads(profile.mail_data.read_text(encoding="utf-8"))
    page, signals = data["page"], data["signals"]
    url = os.environ.get("PAGE_URL", "")
    recipient = os.environ.get(profile.email_to_env, "") if profile.email_to_env else ""
    recipient = recipient or user  # until a recipient is set, the sender gets it: a safe way to test

    message = EmailMessage()
    message["Subject"] = f"Tyyni · {page['date_short']}"
    message["From"] = formataddr(("Tyyni", user))
    message["To"] = recipient
    message.set_content(plain_email(page, signals, url))
    message.add_alternative(
        render(EMAIL_TEMPLATE.read_text(encoding="utf-8"), {**page, "url": url}, signals), subtype="html")
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=60) as smtp:
        smtp.login(user, password)
        smtp.send_message(message)
    print(f"  emailed N:o {page['number']}{' to the sender (test)' if recipient == user else ''}")
    return 0


# --------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------

def check_due() -> int:
    """List the profiles whose latest edition is missing from their page. The
    workflow asks this at every look-in, so a delayed or dropped run never
    costs an edition: whichever look-in comes first after an edition hour
    publishes it."""
    now, due = datetime.now(TZ), []
    for profile in PROFILES.values():
        current, _ = editions(now, profile.editions)
        missing = not already_published(profile, current)
        print(f"{profile.key}: {profile.editions[current.hour]} {slot_id(current)}: "
              f"{'due' if missing else 'already published'}")
        if missing:
            due.append(profile.key)
    write_outputs({"profiles": " ".join(due)})
    return 0


def publish(profile: Profile) -> int:
    today = datetime.now(TZ)
    current, upcoming = editions(today, profile.editions)
    print(f"Tyyni · {profile.key} · {long_date(today)} · {profile.editions[current.hour]}")

    template = TEMPLATE.read_text(encoding="utf-8")
    weather = fetch_weather()
    print(f"  weather {degrees(weather.now)}, {weather.condition}")

    news = fetch_news(profile)
    for section in profile.sections:
        print(f"  {section.key:<15} {len(news[section.key]):>3} items")
    if sum(1 for items in news.values() if items) < profile.min_sections:
        raise RuntimeError("too few sources answered; keeping the previous edition")

    edition = compose(profile, briefing(profile, today, current.hour, weather, news))
    page, signals = build_page(profile, today, current, upcoming, edition_number(profile),
                               weather, news, edition)
    profile.output.parent.mkdir(parents=True, exist_ok=True)
    profile.output.write_text(render(template, page, signals), encoding="utf-8")
    morning = current.hour == min(profile.editions)
    if profile.notify == "email":
        profile.mail_data.write_text(json.dumps({"page": page, "signals": signals}, ensure_ascii=False),
                                     encoding="utf-8")
    write_outputs({
        f"{profile.key}_number": page["number"],
        f"{profile.key}_date": page["date_long"],
        f"{profile.key}_atmosphere": page["atmosphere"],
        f"{profile.key}_morning": "true" if morning else "false",
    })
    print(f"  wrote {profile.output.relative_to(ROOT)}: N:o {page['number']}; next edition {page['next_edition']}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--profile", choices=PROFILES, default="main", help="write this profile's edition")
    group.add_argument("--due", action="store_true", help="list the profiles whose latest edition is missing")
    group.add_argument("--send", choices=PROFILES, help="email this profile's latest edition")
    args = parser.parse_args(argv)
    if args.due:
        return check_due()
    if args.send:
        return send_email(PROFILES[args.send])
    return publish(PROFILES[args.profile])


if __name__ == "__main__":
    sys.exit(main())
