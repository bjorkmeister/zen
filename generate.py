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
    python generate.py --apply-settings save the editor's settings from ISSUE_BODY

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
import math
import os
import re
import smtplib
import sys
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
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
    paywalled: bool = False  # shown as "tilaajille" and not linked


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
    links: bool = False              # the source name opens the original story
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
    editions={6: "aamupainos"},
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
        "The sheet appears once a day, at 6.00; the reader reads it once, slowly, with their first coffee, and "
        "then closes the screen. Your task is to give them clarity and calm: the few things that matter, why "
        "they matter, and nothing else."
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
    links=True,
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
            Feed("Helsingin Sanomat", "https://www.hs.fi/rss/ruoka.xml", paywalled=True),
            Feed("Eater", "https://www.eater.com/rss/index.xml"),
            Feed("The Guardian", "https://www.theguardian.com/food/rss"),
        )),
        Section("trendit", "Trendit", (
            Feed("Vogue", "https://www.vogue.com/feed/rss"),
            Feed("The Guardian", "https://www.theguardian.com/fashion/rss"),
            Feed("Harper's Bazaar", "https://www.harpersbazaar.com/rss/all.xml/"),
        )),
        Section("kulttuuri", "Kulttuuri", (
            Feed("Helsingin Sanomat", "https://www.hs.fi/rss/kulttuuri.xml", paywalled=True),
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
# Editor's desk: settings that steer the editions, and a log of each edition
# --------------------------------------------------------------------------

DESK_HISTORY = 120   # editions kept in each profile's log
CRITIC_MODELS = ("gemini-3.8-flash", "gemini-4-hydrogen")

# Each knob runs from -2 to +2. 0 leaves the profile's own voice untouched;
# the ends tell the writer what to favour, and the critic measures the same axes.
KNOBS = {
    "tone": ("Tunne", "Fakta",
             "emotional resonance and lived human experience",
             "facts, figures and verifiable detail"),
    "politics": ("Vasen", "Oikea",
                 "perspectives the political left tends to stress – equality, labour, public services, climate and regulation",
                 "perspectives the political right tends to stress – markets, enterprise, security, fiscal discipline and individual responsibility"),
    "outlook": ("Huoli", "Toivo",
                "risks, costs and what could go wrong",
                "progress, solutions and what is going right"),
    "scope": ("Paikallinen", "Globaali",
              "Finland, the Nordic countries and Europe",
              "the wider world beyond Europe"),
    "depth": ("Tiivis", "Syvä",
              "concision: shorter sentences and the lower end of every length range",
              "context and nuance: the upper end of every length range"),
    "novelty": ("Vakiintunut", "Heikot signaalit",
                "established, well-confirmed developments",
                "early, weak signals and emerging ideas"),
}
WEIGHTS = ("korosta", "normaali", "hiljennä")   # boost, normal, silence


def settings_file() -> Path:
    return ROOT / "toimitus" / "asetukset.json"


def desk_log(profile: Profile) -> Path:
    return ROOT / "toimitus" / f"{profile.key}.json"


def source_key(section: Section, feed: Feed | None = None) -> str:
    return f"{section.key}/{feed.name if feed else 'Linked Events'}"


def section_sources(section: Section) -> list[str]:
    return ([source_key(section)] if section.events else []) + [source_key(section, f) for f in section.feeds]


def default_settings() -> dict:
    return {"profiles": {
        key: {"knobs": {knob: 0 for knob in KNOBS},
              "sources": {s: "normaali" for section in profile.sections for s in section_sources(section)}}
        for key, profile in PROFILES.items()}}


def validate_settings(raw) -> dict:
    """Complete, checked settings from untrusted input; ValueError on anything unexpected."""
    if not isinstance(raw, dict) or not isinstance(raw.get("profiles"), dict):
        raise ValueError("settings need a 'profiles' object")
    settings = default_settings()
    for key, given in raw["profiles"].items():
        if key not in PROFILES or not isinstance(given, dict):
            raise ValueError(f"unknown profile: {key}")
        mine = settings["profiles"][key]
        for knob, value in (given.get("knobs") or {}).items():
            if knob not in KNOBS:
                raise ValueError(f"unknown knob: {knob}")
            if not isinstance(value, int) or isinstance(value, bool) or not -2 <= value <= 2:
                raise ValueError(f"{key}/{knob} must be a whole number from -2 to 2")
            mine["knobs"][knob] = value
        for source, weight in (given.get("sources") or {}).items():
            if source not in mine["sources"]:
                raise ValueError(f"unknown source: {key}/{source}")
            if weight not in WEIGHTS:
                raise ValueError(f"{key}/{source} must be one of {', '.join(WEIGHTS)}")
            mine["sources"][source] = weight
        for section in PROFILES[key].sections:
            if all(mine["sources"][s] == "hiljennä" for s in section_sources(section)):
                raise ValueError(f"{key}: every source of {section.key} is silenced")
    return settings


def load_settings() -> dict:
    try:
        return validate_settings(json.loads(settings_file().read_text(encoding="utf-8")))
    except FileNotFoundError:
        return default_settings()
    except (ValueError, json.JSONDecodeError) as error:
        print(f"  ! editor's settings ignored: {error}", file=sys.stderr)
        return default_settings()


def knob_instructions(knobs: dict[str, int]) -> list[str]:
    lines = []
    for knob, value in knobs.items():
        if not value:
            continue
        low, high = KNOBS[knob][2], KNOBS[knob][3]
        strength = "Slightly" if abs(value) == 1 else "Clearly"
        aim = low if value < 0 else high
        if knob == "depth":
            lines.append(f"- {strength} favour {aim}.")
        else:
            lines.append(f"- {strength} favour {aim} when choosing items and in how you frame them.")
        if knob == "politics":
            lines[-1] += " This shifts emphasis only: facts, fairness and accuracy stay exactly as they are."
    return lines


# --------------------------------------------------------------------------
# Fetching
# --------------------------------------------------------------------------

def fetch(url: str, attempts: int = 3, timeout: float = 20.0, agent: str = "") -> bytes:
    """GET a URL, retrying transient failures with a short backoff."""
    request = urllib.request.Request(url, headers={"User-Agent": agent or USER_AGENT})
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
    """Today's weather in Olari. The sun is always known (it is calculated);
    the rest is None when no weather service answered."""
    sunrise: datetime
    sunset: datetime
    daylight_s: float
    daylight_change_s: float
    now: float | None = None
    feels_like: float | None = None
    high: float | None = None
    low: float | None = None
    condition: str | None = None
    wind_ms: float | None = None
    precipitation_mm: float | None = None
    precipitation_chance: int | None = None
    source: str = ""


def sun_times(day: date) -> tuple[datetime, datetime]:
    """Sunrise and sunset in Olari, from NOAA's solar equations (within a
    few minutes of the published times), so daylight never depends on a service."""
    lat, lon = math.radians(OLARI["latitude"]), OLARI["longitude"]
    g = 2 * math.pi / 365 * (day.timetuple().tm_yday - 1)
    eqtime = 229.18 * (0.000075 + 0.001868 * math.cos(g) - 0.032077 * math.sin(g)
                       - 0.014615 * math.cos(2 * g) - 0.040849 * math.sin(2 * g))
    decl = (0.006918 - 0.399912 * math.cos(g) + 0.070257 * math.sin(g) - 0.006758 * math.cos(2 * g)
            + 0.000907 * math.sin(2 * g) - 0.002697 * math.cos(3 * g) + 0.00148 * math.sin(3 * g))
    cos_ha = (math.cos(math.radians(90.833)) / (math.cos(lat) * math.cos(decl))
              - math.tan(lat) * math.tan(decl))
    ha = math.degrees(math.acos(max(-1.0, min(1.0, cos_ha))))
    midnight = datetime(day.year, day.month, day.day, tzinfo=timezone.utc)
    rise = midnight + timedelta(minutes=720 - 4 * (lon + ha) - eqtime)
    set_ = midnight + timedelta(minutes=720 - 4 * (lon - ha) - eqtime)
    return rise.astimezone(TZ), set_.astimezone(TZ)


def daylight(day: date) -> float:
    rise, set_ = sun_times(day)
    return (set_ - rise).total_seconds()


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

# MET Norway symbol codes, without their _day/_night/_polartwilight suffix.
MET_FI = {
    "clearsky": "selkeää", "fair": "enimmäkseen selkeää", "partlycloudy": "puolipilvistä",
    "cloudy": "pilvistä", "fog": "sumua",
    "lightrain": "heikkoa vesisadetta", "rain": "vesisadetta", "heavyrain": "voimakasta vesisadetta",
    "lightrainshowers": "heikkoja sadekuuroja", "rainshowers": "sadekuuroja", "heavyrainshowers": "rankkoja sadekuuroja",
    "lightsleet": "heikkoa räntäsadetta", "sleet": "räntäsadetta", "heavysleet": "voimakasta räntäsadetta",
    "lightsleetshowers": "heikkoja räntäkuuroja", "sleetshowers": "räntäkuuroja", "heavysleetshowers": "voimakkaita räntäkuuroja",
    "lightsnow": "heikkoa lumisadetta", "snow": "lumisadetta", "heavysnow": "voimakasta lumisadetta",
    "lightsnowshowers": "heikkoja lumikuuroja", "snowshowers": "lumikuuroja", "heavysnowshowers": "voimakkaita lumikuuroja",
}


def wind_chill(temp: float, wind_ms: float) -> float:
    """The Finnish Meteorological Institute's feels-like temperature."""
    if temp > 10 or wind_ms < 1.3:
        return temp
    v = (wind_ms * 3.6) ** 0.16
    return 13.12 + 0.6215 * temp - 11.37 * v + 0.3965 * temp * v


def open_meteo() -> dict:
    params = {
        **OLARI,
        "timezone": "Europe/Helsinki",
        "wind_speed_unit": "ms",
        "forecast_days": 1,
        "current": "temperature_2m,apparent_temperature,weather_code,wind_speed_10m",
        "daily": "weather_code,temperature_2m_max,temperature_2m_min,precipitation_sum,"
                 "precipitation_probability_max,wind_speed_10m_max",
    }
    url = "https://api.open-meteo.com/v1/forecast?" + urllib.parse.urlencode(params)
    data = json.loads(fetch(url, attempts=2, timeout=15.0))
    current, daily = data["current"], data["daily"]
    return {
        "now": current["temperature_2m"], "feels_like": current["apparent_temperature"],
        "high": daily["temperature_2m_max"][0], "low": daily["temperature_2m_min"][0],
        "condition": WMO_FI.get(daily["weather_code"][0], "vaihtelevaa säätä"),
        "wind_ms": daily["wind_speed_10m_max"][0], "precipitation_mm": daily["precipitation_sum"][0] or 0.0,
        "precipitation_chance": daily["precipitation_probability_max"][0], "source": "Open-Meteo",
    }


def met_norway(today: date) -> dict:
    url = ("https://api.met.no/weatherapi/locationforecast/2.0/compact?"
           + urllib.parse.urlencode({"lat": OLARI["latitude"], "lon": OLARI["longitude"]}))
    # MET Norway asks every client to identify itself rather than pose as a browser.
    data = json.loads(fetch(url, attempts=2, timeout=15.0, agent="Tyyni/1.0 personal daily digest"))
    series = data["properties"]["timeseries"]
    first = series[0]["data"]
    now, wind = first["instant"]["details"]["air_temperature"], first["instant"]["details"]["wind_speed"]
    today_entries = [t for t in series
                     if datetime.fromisoformat(t["time"].replace("Z", "+00:00")).astimezone(TZ).date() == today]
    temps = [t["data"]["instant"]["details"]["air_temperature"] for t in today_entries] or [now]
    winds = [t["data"]["instant"]["details"]["wind_speed"] for t in today_entries] or [wind]
    rain = sum(t["data"].get("next_1_hours", {}).get("details", {}).get("precipitation_amount", 0.0)
               for t in today_entries)
    summary = first.get("next_1_hours") or first.get("next_6_hours") or {}
    symbol = summary.get("summary", {}).get("symbol_code", "").split("_")[0]
    condition = MET_FI.get(symbol) or ("ukkosta" if "thunder" in symbol else "vaihtelevaa säätä")
    return {
        "now": now, "feels_like": wind_chill(now, wind), "high": max(temps), "low": min(temps),
        "condition": condition, "wind_ms": max(winds), "precipitation_mm": round(rain, 1),
        "precipitation_chance": None, "source": "MET Norway",
    }


def fetch_weather() -> Weather:
    """Today's weather: Open-Meteo, else MET Norway, else only the sun. Open-Meteo
    has stalled on GitHub's shared runners more than once; an edition should
    never wait on a weather service."""
    today = datetime.now(TZ).date()
    sunrise, sunset = sun_times(today)
    sky: dict = {}
    for name, source in (("Open-Meteo", open_meteo), ("MET Norway", lambda: met_norway(today))):
        try:
            sky = source()
            break
        except Exception as error:
            print(f"  ! weather from {name}: {type(error).__name__}: {error}", file=sys.stderr, flush=True)
    return Weather(sunrise=sunrise, sunset=sunset, daylight_s=daylight(today),
                   daylight_change_s=daylight(today) - daylight(today - timedelta(days=1)), **sky)


@dataclass(frozen=True)
class Item:
    id: str
    section: str
    source: str
    title: str
    summary: str
    when: str      # "5 h" for news, "ti 18.11. klo 19.00" for events
    url: str = ""
    paywalled: bool = False
    feed: str = ""        # source key, e.g. "design/Dezeen"
    boosted: bool = False


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


def web_link(value) -> str:
    """Only plain web addresses ever become links."""
    value = str(value or "").strip()
    return value if value.startswith(("https://", "http://")) else ""


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
            "url": web_link(entry.get("link")),
            "paywalled": feed.paywalled,
            "age": max(0, age),
        })
    entries.sort(key=lambda e: e["age"])
    return [{**{k: v for k, v in e.items() if k != "age"}, "when": f"{round(e['age'])} h"}
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
        info = event.get("info_url") or {}
        entries.append({
            "source": venue or "Helsinki",
            "title": name,
            "summary": shorten(finnish(event.get("short_description")), SUMMARY_CHARS),
            # the organiser's own page, or else the city's event page
            "url": web_link(info.get("fi") or info.get("en") or info.get("sv"))
                   or f"https://tapahtumat.hel.fi/fi/events/{urllib.parse.quote(str(event.get('id', '')), safe=':')}",
            "when": f"{WEEKDAYS_SHORT[start.weekday()]} {start.day}.{start.month}. klo {clock(start)}",
        })
    return entries[:MAX_EVENTS]


def fetch_news(profile: Profile, weights: dict[str, str] | None = None) -> dict[str, list[Item]]:
    """The profile's material by section. Silenced sources are not read at all;
    items from boosted sources are marked for the writer."""
    weights = weights or {}
    now_ts, now = time.time(), datetime.now(TZ)
    jobs: list[tuple[Section, str, Callable[[], list[dict]]]] = []
    for section in profile.sections:
        if section.events:
            jobs.append((section, source_key(section), lambda: read_events(now)))
        for feed in section.feeds:
            jobs.append((section, source_key(section, feed),
                         lambda feed=feed: read_feed(feed, now_ts, profile.window_hours)))
    jobs = [job for job in jobs if weights.get(job[1]) != "hiljennä"]
    with ThreadPoolExecutor(max_workers=len(jobs)) as pool:
        results = list(pool.map(lambda job: job[2](), jobs))

    news: dict[str, list[Item]] = {section.key: [] for section in profile.sections}
    seen: set[str] = set()
    for (section, key, _), entries in zip(jobs, results):
        for entry in entries:
            fingerprint = re.sub(r"\W+", "", entry["title"].lower())
            if fingerprint in seen:
                continue
            seen.add(fingerprint)
            bucket = news[section.key]
            bucket.append(Item(id=f"{section.key[:2].upper()}{len(bucket) + 1}", section=section.key,
                               feed=key, boosted=weights.get(key) == "korosta", **entry))
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
atmosphere: exactly one sentence, at most 30 words. The weather in Espoo as it stands at this edition's hour and the rhythm of daylight, including the current temperature from the data. Evocative but concrete, true to the data; no stock phrases. If the briefing says the weather services did not answer, describe only the light and the season, with no temperature or rain."""

ZEN = """\
zen_anchor: exactly one sentence, at most 20 words. Timeless, quiet and concrete; it may draw on today's season or light in Espoo. It restores perspective and gently turns the reader toward the rest of their real day – or, in an evening edition, toward rest – without mentioning screens, news, reading or this page, and without quotations, attributed sayings or familiar aphorisms."""


def system_prompt(profile: Profile, settings: dict | None = None) -> str:
    keys = ", ".join(section.key for section in profile.sections)
    example = f"{profile.sections[0].key[:2].upper()}3"
    signals = (
        f"signals: one entry per domain ({keys}), each about the single most worthwhile item in that domain's material.\n"
        f"- source_id: the id of the item you chose, exactly as given (for example \"{example}\").\n"
        "- title: a short Finnish headline, at most 8 words, sentence case, no full stop.\n"
        f"- body: exactly two sentences. First, what happened, concretely and with the actor named. Second, {profile.signal_why}. "
        "Objective; no praise words.\n"
        "- why: one Finnish sentence for the editor's log, never printed: why this item rather than the others.\n"
        "- considered: the ids of up to three other items you weighed for this domain.\n"
        "If a domain's material is thin, choose its least trivial item and state plainly why it matters. Never pad."
    )
    if profile.signal_notes:
        signals += "\n" + profile.signal_notes
    editor = knob_instructions((settings or {}).get("knobs", {}))
    if any(weight == "korosta" for weight in (settings or {}).get("sources", {}).values()):
        editor.append("- Items marked ★ come from sources the editor wants to see more of: prefer them when "
                      "they are about as worthwhile as the alternatives.")
    principles = profile.principles + "\n" + SHARED_PRINCIPLES
    if editor:
        principles += "\n\nThe managing editor's current guidance\n" + "\n".join(editor)
    return "\n\n".join((
        profile.reader,
        LANGUAGE,
        "Editorial principles\n" + principles,
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
            "why": {"type": "string"},
            "considered": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["source_id", "title", "body", "why", "considered"],
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
    if weather.now is None:
        sky = ["- sääpalvelut eivät vastanneet: kuvaa vain valoa ja vuodenaikaa, älä mainitse lämpötilaa tai sadetta"]
    else:
        rain = f"{weather.precipitation_mm or 0:.1f} mm".replace(".", ",")
        if weather.precipitation_chance is not None:
            rain += f", todennäköisyys {weather.precipitation_chance} %"
        sky = [
            f"- nyt {degrees(weather.now)}, tuntuu kuin {degrees(weather.feels_like)}",
            f"- päivän ylin {degrees(weather.high)}, alin {degrees(weather.low)}; {weather.condition}",
            f"- sade {rain}; tuuli enimmillään {round(weather.wind_ms or 0)} m/s",
        ]
    lines = [
        f"Päivä: {long_date(today)}, vuoden {today.timetuple().tm_yday}. päivä.",
        f"Painos: {profile.editions[edition_hour]}, luetaan noin kello {edition_hour}.00.",
        "",
        f"Sää Olarissa, Espoossa tänään{f' ({weather.source})' if weather.source else ''}:",
        *sky,
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
            text = f"{'★ ' if item.boosted else ''}[{item.id}] {item.source} · {item.when} – {item.title}"
            if item.summary:
                text += f" – {item.summary}"
            lines.append(text)
    return "\n".join(lines)


def compose(profile: Profile, prompt: str, settings: dict | None = None) -> tuple[dict, dict]:
    """Ask each model in turn until one returns a complete edition. Returns the
    edition and the facts of the run (model, time, tokens) for the editor's log."""
    from google import genai
    from google.genai import errors, types

    # One attempt per model: a normal edition takes one to two minutes, so a
    # call still running after four has stalled and is handed to the next model.
    client = genai.Client(http_options=types.HttpOptions(
        timeout=240_000,  # milliseconds
        retry_options=types.HttpRetryOptions(attempts=1),
    ))
    config = types.GenerateContentConfig(
        system_instruction=system_prompt(profile, settings),
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
            return edition, {
                "model": model,
                "seconds": round(time.monotonic() - started),
                "tokens": {"in": getattr(usage, "prompt_token_count", None),
                           "out": getattr(usage, "candidates_token_count", None),
                           "thinking": getattr(usage, "thoughts_token_count", None)},
            }
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
# <!-- if key -->shown when the value is non-empty<!-- else -->otherwise<!-- /if -->
# Innermost blocks are resolved first, so blocks can nest.
CONDITION = re.compile(
    r"<!-- if (\w+) -->((?:(?!<!-- if ).)*?)(?:<!-- else -->((?:(?!<!-- if ).)*?))?<!-- /if -->", re.S)
SIGNAL_BLOCK = re.compile(r"<!-- signal -->(.*?)<!-- /signal -->", re.S)


def fill(fragment: str, values: dict[str, str]) -> str:
    while CONDITION.search(fragment):
        fragment = CONDITION.sub(lambda m: m.group(2) if values.get(m.group(1)) else (m.group(3) or ""), fragment)

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
    chosen = {item.id: item for items in news.values() for item in items}
    page = {
        **{name: getattr(profile.palette, name) for name in ("paper", "ink", "stone", "rule")},
        "slot": slot_id(current),
        "weather_source": weather.source,
        "next_edition": next_edition_text(today, upcoming),
        "date_long": long_date(today),
        "date_short": short_date(today),
        "number": str(number),
        "temp_now": degrees(weather.now) if weather.now is not None else "",
        "temp_high": degrees(weather.high) if weather.high is not None else "",
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
        item = chosen.get(str(signal.get("source_id", "")).strip())
        signals.append({
            "signal_label": section.label,
            "signal_source": item.source if item else "",
            "signal_url": item.url if item and profile.links and not item.paywalled else "",
            "signal_paywalled": "1" if item and profile.links and item.paywalled else "",
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
# The critic and the editor's log
# --------------------------------------------------------------------------

CRITIC_PROMPT = """\
You are the managing editor of Tyyni, a calm one-page digest in Finnish. Review the finished edition below with a neutral, exact eye. Judge only what is on the page and the source material shown.

Score each axis with a whole number from -2 to +2:
- tone: -2 emotional … +2 factual
- politics: -2 leans left … 0 balanced or apolitical … +2 leans right
- outlook: -2 worried … +2 hopeful
- scope: -2 local (Finland, the Nordics, Europe) … +2 global
- depth: -2 terse … +2 deep
- novelty: -2 established … +2 early, weak signals
Also give:
- factuality: 1–10, how well every claim is supported by the source material (10 = fully supported, nothing invented).
- freshness: 1–10, how little it repeats the previous edition's headlines (10 = nothing repeated).
- cliches: up to five stock phrases or clichés found in the edition, quoted exactly; none if there are none.
- regions: how many of the chosen items concern Finland, the rest of Europe, North America, and elsewhere.
- critique: one sentence in Finnish naming the single most useful improvement for the next edition."""

CRITIC_AXES = tuple(KNOBS)


def critic_schema() -> dict:
    axis = {"type": "integer"}
    regions = {k: {"type": "integer"} for k in ("finland", "europe", "north_america", "elsewhere")}
    return {
        "type": "object",
        "properties": {
            **{name: axis for name in CRITIC_AXES},
            "factuality": {"type": "integer"},
            "freshness": {"type": "integer"},
            "cliches": {"type": "array", "items": {"type": "string"}},
            "regions": {"type": "object", "properties": regions, "required": list(regions),
                        "additionalProperties": False},
            "critique": {"type": "string"},
        },
        "required": [*CRITIC_AXES, "factuality", "freshness", "cliches", "regions", "critique"],
        "additionalProperties": False,
    }


def critic_material(page: dict[str, str], signals: list[dict[str, str]], chosen: list[Item | None],
                    previous_titles: list[str]) -> str:
    lines = ["EDITION", page["atmosphere"], "", page["synthesis"], ""]
    for signal in signals:
        lines.append(f"{signal['signal_label']}: {signal['signal_title']}. {signal['signal_body']}")
    lines += ["", page["reflection"], "", page["zen_anchor"], "", "SOURCE MATERIAL BEHIND THE SIGNALS"]
    for item in chosen:
        if item:
            lines.append(f"- {item.source}: {item.title} – {item.summary or '(no summary)'}")
    lines += ["", "PREVIOUS EDITION'S HEADLINES"] + [f"- {t}" for t in previous_titles or ["(none)"]]
    return "\n".join(lines)


def critique(material: str) -> dict | None:
    """A second, cheaper model's review of the edition. Never stops a publication."""
    try:
        from google import genai
        from google.genai import types
        client = genai.Client(http_options=types.HttpOptions(
            timeout=120_000, retry_options=types.HttpRetryOptions(attempts=1)))
        config = types.GenerateContentConfig(
            system_instruction=CRITIC_PROMPT,
            response_mime_type="application/json",
            response_json_schema=critic_schema(),
            thinking_config=types.ThinkingConfig(thinking_level=types.ThinkingLevel.LOW),
            max_output_tokens=8192,
        )
    except Exception as error:
        print(f"  ! critic unavailable: {error}", file=sys.stderr)
        return None
    for model in CRITIC_MODELS:
        try:
            response = client.models.generate_content(model=model, contents=material, config=config)
            review = json.loads(response.text or "")
        except Exception as error:
            print(f"  ! critic {model}: {type(error).__name__}: {error}", file=sys.stderr)
            continue
        clamp = lambda value, low, high: max(low, min(high, int(value))) if isinstance(value, (int, float)) else None
        review.update({name: clamp(review.get(name), -2, 2) for name in CRITIC_AXES})
        review["factuality"] = clamp(review.get("factuality"), 1, 10)
        review["freshness"] = clamp(review.get("freshness"), 1, 10)
        review["cliches"] = [str(c)[:120] for c in (review.get("cliches") or [])][:5]
        review["critique"] = str(review.get("critique", ""))[:400]
        review["model"] = model
        print(f"  critic {model}: factuality {review['factuality']}, "
              + ", ".join(f"{a} {review[a]:+d}" for a in CRITIC_AXES if review[a] is not None))
        return review
    return None


def read_log(profile: Profile) -> dict:
    try:
        log = json.loads(desk_log(profile).read_text(encoding="utf-8"))
        if isinstance(log, dict) and isinstance(log.get("editions"), list):
            return log
    except (FileNotFoundError, json.JSONDecodeError):
        pass
    return {"profile": profile.key, "editions": []}


def log_edition(profile: Profile, record: dict) -> None:
    log = read_log(profile)
    log["editions"] = (log["editions"] + [record])[-DESK_HISTORY:]
    desk_log(profile).parent.mkdir(parents=True, exist_ok=True)
    desk_log(profile).write_text(json.dumps(log, ensure_ascii=False, indent=1), encoding="utf-8")


def edition_record(profile: Profile, page: dict[str, str], current: datetime, news: dict[str, list[Item]],
                   edition: dict, meta: dict, knobs: dict[str, int], review: dict | None) -> dict:
    items = {item.id: item for bucket in news.values() for item in bucket}
    seen: dict[str, int] = {}
    for item in items.values():
        seen[item.feed] = seen.get(item.feed, 0) + 1
    chosen = []
    for section in profile.sections:
        signal = edition["signals"][section.key]
        item = items.get(str(signal.get("source_id", "")).strip())
        chosen.append({
            "section": section.key,
            "label": section.label,
            "feed": item.feed if item else "",
            "source": item.source if item else "",
            "title": tidy(signal["title"]).rstrip("."),
            "url": item.url if item else "",
            "why": SPACE.sub(" ", str(signal.get("why", ""))).strip(),
            "considered": len([c for c in signal.get("considered") or [] if c in items]),
            "available": len(news[section.key]),
        })
    return {
        "number": int(page["number"]),
        "slot": slot_id(current),
        "weather": page.get("weather_source", ""),
        "written": datetime.now(TZ).strftime("%Y-%m-%dT%H:%M"),
        **meta,
        "knobs": dict(knobs),
        "seen": dict(sorted(seen.items())),
        "chosen": chosen,
        "critique": review,
    }


def apply_settings() -> int:
    """Save the editor's settings from the body of a GitHub issue (ISSUE_BODY).
    Only a JSON block is read; anything that does not validate is rejected."""
    body = os.environ.get("ISSUE_BODY", "")
    block = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", body, re.S)
    try:
        settings = validate_settings(json.loads(block.group(1) if block else body))
    except (ValueError, json.JSONDecodeError) as error:
        print(f"::error::Settings not saved: {error}")
        return 1
    settings_file().parent.mkdir(parents=True, exist_ok=True)
    settings_file().write_text(json.dumps(settings, ensure_ascii=False, indent=1, sort_keys=True) + "\n",
                               encoding="utf-8")
    for key, mine in settings["profiles"].items():
        knobs = ", ".join(f"{k} {v:+d}" for k, v in mine["knobs"].items() if v) or "neutral"
        changed = [f"{s} {w}" for s, w in mine["sources"].items() if w != "normaali"]
        print(f"{key}: knobs {knobs}; sources {', '.join(changed) or 'all normal'}")
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
    print(f"  weather: {weather.source or 'none (sun only)'}"
          + (f", {degrees(weather.now)}, {weather.condition}" if weather.now is not None else ""))

    settings = load_settings()["profiles"][profile.key]
    news = fetch_news(profile, settings["sources"])
    for section in profile.sections:
        print(f"  {section.key:<15} {len(news[section.key]):>3} items")
    if sum(1 for items in news.values() if items) < profile.min_sections:
        raise RuntimeError("too few sources answered; keeping the previous edition")

    edition, meta = compose(profile, briefing(profile, today, current.hour, weather, news), settings)
    page, signals = build_page(profile, today, current, upcoming, edition_number(profile),
                               weather, news, edition)
    profile.output.parent.mkdir(parents=True, exist_ok=True)
    profile.output.write_text(render(template, page, signals), encoding="utf-8")

    items = {item.id: item for bucket in news.values() for item in bucket}
    chosen = [items.get(str(edition["signals"][s.key].get("source_id", "")).strip()) for s in profile.sections]
    previous = read_log(profile)["editions"][-1:]
    previous_titles = [c["title"] for c in previous[0]["chosen"]] if previous else []
    review = critique(critic_material(page, signals, chosen, previous_titles))
    log_edition(profile, edition_record(profile, page, current, news, edition, meta, settings["knobs"], review))
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
    group.add_argument("--apply-settings", action="store_true", help="save the editor's settings from ISSUE_BODY")
    args = parser.parse_args(argv)
    if args.due:
        return check_due()
    if args.apply_settings:
        return apply_settings()
    if args.send:
        return send_email(PROFILES[args.send])
    return publish(PROFILES[args.profile])


if __name__ == "__main__":
    sys.exit(main())
