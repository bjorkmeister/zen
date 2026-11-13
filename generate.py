#!/usr/bin/env python3
"""Aamun arkki: a single-edition morning broadsheet for Espoo.

At 6.00, 14.00 and 20.00 this script
  1. reads today's weather and daylight for Espoo from Open-Meteo,
  2. reads the last day and a half of a few curated RSS feeds in five domains,
  3. asks Gemini to distil them into one calm page in Finnish, and
  4. renders template.html into index.html.

There is no state: every run starts from nothing and overwrites index.html.
If any required step fails the script exits non-zero and writes nothing, so
yesterday's edition stays up rather than a broken one going out.

Requires GEMINI_API_KEY. Dependencies: google-genai, feedparser.
"""

from __future__ import annotations

import calendar
import html
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import feedparser
from google import genai
from google.genai import errors, types

ROOT = Path(__file__).resolve().parent
TEMPLATE = ROOT / "template.html"
OUTPUT = ROOT / "index.html"

TZ = ZoneInfo("Europe/Helsinki")
ESPOO = {"latitude": 60.2055, "longitude": 24.6559}

# Edition hours in Helsinki time and their names. Keep in step with the cron
# lines in .github/workflows/morning_edition.yml, which start each run ten
# minutes early. The first edition is the one that sends the morning link.
EDITIONS = {6: "aamupainos", 14: "iltapäiväpainos", 20: "iltapainos"}
LEAD = timedelta(minutes=20)  # a run this close before an edition hour belongs to it

# Tried in order. Carbon writes best but needs a paid key; Flash and Hydrogen
# are on the free tier, so the paper still appears if Carbon is unavailable.
MODELS = ("gemini-4-carbon", "gemini-3.8-flash", "gemini-4-hydrogen")
USER_AGENT = "Mozilla/5.0 (compatible; AamunArkki/1.0; personal morning digest)"
WINDOW_HOURS = 36       # "yesterday", with slack for time zones and late publishing
ITEMS_PER_FEED = 10
SUMMARY_CHARS = 320
MIN_SECTIONS_WITH_NEWS = 4


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
    feeds: tuple[Feed, ...]


SECTIONS: tuple[Section, ...] = (
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
)


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
        **ESPOO,
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
    age_hours: int


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


def read_feed(feed: Feed, now: float) -> list[dict]:
    """Recent entries from one feed, newest first. A failing feed yields nothing."""
    try:
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
        if not -1 <= age <= WINDOW_HOURS:
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
            "age_hours": max(0, round(age)),
        })
    entries.sort(key=lambda e: e["age_hours"])
    return entries[:ITEMS_PER_FEED]


def fetch_news() -> dict[str, list[Item]]:
    now = time.time()
    jobs = [(section, feed) for section in SECTIONS for feed in section.feeds]
    with ThreadPoolExecutor(max_workers=len(jobs)) as pool:
        results = list(pool.map(lambda job: read_feed(job[1], now), jobs))

    news: dict[str, list[Item]] = {section.key: [] for section in SECTIONS}
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


def long_date(day: datetime) -> str:
    return f"{WEEKDAYS[day.weekday()].capitalize()} {day.day}. {MONTHS[day.month - 1]} {day.year}"


def editions(now: datetime) -> tuple[datetime, datetime]:
    """The edition this run belongs to, and the one after it."""
    anchor = now + LEAD
    slots = sorted(
        (anchor + timedelta(days=offset)).replace(hour=hour, minute=0, second=0, microsecond=0)
        for offset in (-1, 0, 1) for hour in EDITIONS
    )
    current = max(slot for slot in slots if slot <= anchor)
    upcoming = min(slot for slot in slots if slot > anchor)
    return current, upcoming


# --------------------------------------------------------------------------
# Composition
# --------------------------------------------------------------------------

SYSTEM_PROMPT = """\
You are the sole editor of Aamun arkki ("the morning sheet"), a one-page broadsheet with exactly one reader: a senior Finnish leader in Espoo who works where technology, organisations and strategy meet. The sheet appears three times a day – morning (6.00), afternoon (14.00) and evening (20.00) – and the reader reads each edition once, slowly, and then closes the screen. The briefing says which edition this is; let the atmosphere and the zen anchor fit that hour of the day. Your task is to give them clarity and calm: the few things that matter, why they matter, and nothing else.

Language
Write every field in Finnish: natural, precise, contemporary written Finnish of the kind the best Finnish long-form editors produce. Use Finnish syntax, not translated English. Prefer a good Finnish word to an anglicism, an active verb to a stack of nouns, and avoid officialese such as "toimesta", "omata", "suorittaa" used as a filler verb, or "tulee olemaan". Keep proper names in their original form. Use Finnish typography: ”lainausmerkit”, ajatusviiva (–), kellonajat pisteellä (8.06), desimaalipilkku, miinusmerkki (−3°).

Editorial principles
- Signal over noise. Choose events with structural consequence: shifts in incentives, capital, regulation, platforms, institutions or power. Pass over product launches, minor deals, rumours, previews, viewing guides, listicles, celebrity, and opinion pieces unless they reveal something structural.
- Use only the material provided for events. Never invent facts, figures, quotes or outcomes. Widely known background may frame an item, but must not add new news. If an item is thin, write less rather than guess.
- Calm and exact. No hype, no alarm, no exclamation marks, no emoji, no markdown, no rhetorical questions.

Fields
atmosphere: exactly one sentence, at most 30 words. The weather in Espoo as it stands at this edition's hour and the rhythm of daylight, including the current temperature from the data. Evocative but concrete, true to the data; no stock phrases.

megatrend_synthesis: one paragraph of 3–4 sentences. Look beneath the individual headlines and name the structural current that links at least two of the domains. Name the mechanism – who gains leverage, which constraint moves, which incentive changes – rather than the topic. Do not summarise the headlines one by one.

signals: one entry per domain (tekniikka, talous, design, media, huippu_urheilu), each about the single most consequential item in that domain's material.
- source_id: the id of the item you chose, exactly as given (for example "TE3").
- title: a short Finnish headline, at most 8 words, sentence case, no full stop.
- body: exactly two sentences. First, what happened, concretely and with the actor named. Second, why it matters structurally. Objective; no praise words.
If a domain's material is thin, choose its least trivial item and state plainly why it matters. Never pad.

executive_reflection: one paragraph of 3–5 sentences for a senior leader. Start from a concrete pattern visible in today's material and draw a first-principles insight about organisations, strategy or systems – for example incentive structures, second-order effects, governance friction, Conway's law, information asymmetry, principal–agent problems, operational leverage, feedback delays or optionality. It must be specific enough that it could not have been written on any other day. Forbidden: management clichés and motivational language (such as "muutos on ainoa pysyvä", "kulttuuri syö strategian aamiaiseksi", ketteryys, synergia, paradigman muutos, mukavuusalue, win-win), lists, advice in the imperative, and questions.

zen_anchor: exactly one sentence, at most 20 words. Timeless, quiet and concrete; it may draw on today's season or light in Espoo. It restores perspective and gently turns the reader toward the rest of their real day – or, in the evening edition, toward rest – without mentioning screens, news, reading or this page, and without quotations, attributed sayings or familiar aphorisms.
"""


def edition_schema() -> dict:
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
    keys = [section.key for section in SECTIONS]
    return {
        "type": "object",
        "properties": {
            "atmosphere": {"type": "string"},
            "megatrend_synthesis": {"type": "string"},
            "signals": {
                "type": "object",
                "properties": {key: signal for key in keys},
                "required": keys,
                "additionalProperties": False,
            },
            "executive_reflection": {"type": "string"},
            "zen_anchor": {"type": "string"},
        },
        "required": ["atmosphere", "megatrend_synthesis", "signals",
                     "executive_reflection", "zen_anchor"],
        "additionalProperties": False,
    }


def briefing(today: datetime, edition_hour: int, weather: Weather, news: dict[str, list[Item]]) -> str:
    rain = f"{weather.precipitation_mm:.1f} mm".replace(".", ",")
    if weather.precipitation_chance is not None:
        rain += f", todennäköisyys {weather.precipitation_chance} %"
    lines = [
        f"Päivä: {long_date(today)}, vuoden {today.timetuple().tm_yday}. päivä.",
        f"Painos: {EDITIONS[edition_hour]}, luetaan noin kello {edition_hour}.00.",
        "",
        "Sää Espoossa tänään (Open-Meteo):",
        f"- nyt {degrees(weather.now)}, tuntuu kuin {degrees(weather.feels_like)}",
        f"- päivän ylin {degrees(weather.high)}, alin {degrees(weather.low)}; {weather.condition}",
        f"- sade {rain}; tuuli enimmillään {round(weather.wind_ms)} m/s",
        f"- aurinko nousee {clock(weather.sunrise)} ja laskee {clock(weather.sunset)}",
        f"- valoisaa {duration(weather.daylight_s)} ({daylight_change(weather.daylight_change_s)} eiliseen)",
        "",
        f"Uutisaineisto, viimeiset {WINDOW_HOURS} tuntia. Muoto: [id] lähde · ikä – otsikko – tiivistelmä.",
    ]
    for section in SECTIONS:
        lines += ["", f"## {section.key} ({section.label})"]
        items = news[section.key]
        if not items:
            lines.append("(ei aineistoa tänään)")
        for item in items:
            text = f"[{item.id}] {item.source} · {item.age_hours} h – {item.title}"
            if item.summary:
                text += f" – {item.summary}"
            lines.append(text)
    return "\n".join(lines)


def compose(prompt: str) -> dict:
    """Ask each model in turn until one returns a complete edition."""
    # One unhurried attempt per model: deep thinking can take minutes, and a
    # timed-out call is better handed to the next model than repeated.
    client = genai.Client(http_options=types.HttpOptions(
        timeout=600_000,  # milliseconds
        retry_options=types.HttpRetryOptions(attempts=1),
    ))
    config = types.GenerateContentConfig(
        system_instruction=SYSTEM_PROMPT,
        response_mime_type="application/json",
        response_json_schema=edition_schema(),
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
        if complete(edition):
            return edition
        print(f"  ! {model}: edition is missing fields", file=sys.stderr)
    raise RuntimeError("no model produced an edition")


def complete(edition: dict) -> bool:
    text_fields = ("atmosphere", "megatrend_synthesis", "executive_reflection", "zen_anchor")
    signals = edition.get("signals") or {}
    return (all(str(edition.get(field, "")).strip() for field in text_fields)
            and all(str((signals.get(section.key) or {}).get(part, "")).strip()
                    for section in SECTIONS for part in ("title", "body")))


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
    repeated = "".join(fill(block.group(1), signal) for signal in signals)
    return fill(template[:block.start()], page) + repeated + fill(template[block.end():], page)


def build_page(today: datetime, upcoming: datetime, number: int, weather: Weather,
               news: dict[str, list[Item]], edition: dict) -> tuple[dict[str, str], list[dict[str, str]]]:
    sources = {item.id: item.source for items in news.values() for item in items}
    page = {
        "next_edition": f"{upcoming.hour:02d}.{upcoming.minute:02d}",
        "date_long": long_date(today),
        "date_short": f"{WEEKDAYS[today.weekday()]} {today.day}.{today.month}.",
        "number": str(number),
        "temp_now": degrees(weather.now),
        "temp_high": degrees(weather.high),
        "sun": f"{clock(weather.sunrise)}–{clock(weather.sunset)}",
        "daylight": duration(weather.daylight_s),
        "daylight_change": daylight_change(weather.daylight_change_s),
        "atmosphere": tidy(edition["atmosphere"]),
        "megatrend_synthesis": tidy(edition["megatrend_synthesis"]),
        "executive_reflection": tidy(edition["executive_reflection"]),
        "zen_anchor": tidy(edition["zen_anchor"]),
    }
    signals = []
    for section in SECTIONS:
        signal = edition["signals"][section.key]
        signals.append({
            "signal_label": section.label,
            "signal_source": sources.get(str(signal.get("source_id", "")).strip(), ""),
            "signal_title": tidy(signal["title"]).rstrip("."),
            "signal_body": tidy(signal["body"]),
        })
    return page, signals


EDITION_NUMBER = re.compile(r'<meta name="edition-number" content="(\d+)">')


def edition_number() -> int:
    """One more than the edition being replaced. Each page records its own
    number, so the count needs no store of its own and skips no numbers when a
    run fails. A page without a number (or no page) makes this N:o 1."""
    try:
        found = EDITION_NUMBER.search(OUTPUT.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return 1
    return int(found.group(1)) + 1 if found else 1


def announce(page: dict[str, str], morning: bool) -> None:
    """Hand the date, the weather sentence and whether this is the morning
    edition (the one that sends the link) to the workflow."""
    target = os.environ.get("GITHUB_OUTPUT")
    if target:
        with open(target, "a", encoding="utf-8") as out:
            out.write(f"date={page['date_long']}\n")
            out.write(f"number={page['number']}\n")
            out.write(f"atmosphere={page['atmosphere']}\n")
            out.write(f"morning={'true' if morning else 'false'}\n")


def main() -> int:
    today = datetime.now(TZ)
    current, upcoming = editions(today)
    print(f"Aamun arkki · {long_date(today)} · {EDITIONS[current.hour]}")

    template = TEMPLATE.read_text(encoding="utf-8")
    weather = fetch_weather()
    print(f"  weather {degrees(weather.now)}, {weather.condition}")

    news = fetch_news()
    for section in SECTIONS:
        print(f"  {section.key:<15} {len(news[section.key]):>3} items")
    if sum(1 for items in news.values() if items) < MIN_SECTIONS_WITH_NEWS:
        raise RuntimeError("too few feeds answered; keeping yesterday's edition")

    edition = compose(briefing(today, current.hour, weather, news))
    page, signals = build_page(today, upcoming, edition_number(), weather, news, edition)
    OUTPUT.write_text(render(template, page, signals), encoding="utf-8")
    announce(page, morning=current.hour == min(EDITIONS))
    print(f"  wrote {OUTPUT.name}: N:o {page['number']}; next edition {page['next_edition']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
