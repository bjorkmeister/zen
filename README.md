# Aamun arkki

A one-page broadsheet that writes itself three times a day. Weather and daylight for Espoo, the structural current beneath the news, five signals, one reflection and one quiet closing line, set like printed paper. No links, no buttons, no archive, no state.

Live: **https://bjorkmeister.github.io/zen/**

<p align="center">
  <img width="400" alt="An edition of Aamun arkki" src="https://github.com/user-attachments/assets/92652652-fe40-4539-972b-14fbdcf9d1a0" />
</p>

## How it works

At 06.00, 14.00 and 20.00 (Helsinki time) a GitHub Action:

1. reads the weather and day length from [Open-Meteo](https://open-meteo.com) (no key needed),
2. reads the last 36 hours of a dozen curated RSS feeds across technology, economy, design, media and sport,
3. asks Gemini to distil them into one calm page in Finnish,
4. renders `template.html` into `index.html`, commits it and publishes it to GitHub Pages,
5. after the morning edition, posts the link in an issue thread, which reaches you by email.

If anything fails, nothing is published and the previous edition stays up. GitHub emails you about the failed run.

| File | Role |
| --- | --- |
| `generate.py` | Fetches the data, calls Gemini and renders the page |
| `template.html` | The page: typography, layout and placeholders |
| `.github/workflows/morning_edition.yml` | The schedule, the commit, the deploy and the notification |
| `index.html` | The current edition, written by the workflow |

Every edition is numbered (*N:o 1, N:o 2, …*). Each page records its own number in a hidden tag, and the next run adds one to it. Only published editions are counted, so a failed run doesn't skip a number. To start counting again from N:o 1, delete `index.html`.

## Run your own

You need a GitHub account and a Gemini API key. It takes about five minutes.

1. **Fork this repository.** Keep it public: GitHub Pages is free for public repositories.
2. **Get a Gemini API key** at [Google AI Studio](https://aistudio.google.com/apikey). The free tier works: the script falls back to Gemini 3.8 Flash. A paid key lets it use Gemini 4 Carbon, which writes better.
3. **Add the key as a secret.** In your fork, open *Settings → Secrets and variables → Actions → New repository secret*. Name it `GEMINI_API_KEY` and paste the key.
4. **Turn on Pages.** Open *Settings → Pages* and set *Source* to **GitHub Actions**.
5. **Turn on Issues.** Forks have them off by default. Open *Settings → General → Features* and tick **Issues**. The morning link is posted there.
6. **Turn on Actions.** Open the **Actions** tab and confirm that you want to run workflows in your fork.
7. **Run the first edition.** On the Actions tab, choose **Morning edition → Run workflow**. It takes about two minutes.

Your page is now at `https://<your-username>.github.io/<repository-name>/`, and it refreshes on schedule from then on.

The first run opens an issue called *Aamun arkki* that @-mentions you. Every morning edition adds a comment to that thread, so you get one email a day. If you don't receive it, check that email is enabled for *Participating and @mentions* in your GitHub notification settings.

## Make it yours

Everything is in two files, near the top.

| To change | Edit |
| --- | --- |
| The location | `ESPOO` (latitude and longitude) in `generate.py`, and the word *Espoo* in `template.html` |
| The news sources | `SECTIONS` in `generate.py`: up to three RSS feeds per domain |
| The editorial voice | `SYSTEM_PROMPT` in `generate.py` |
| The models | `MODELS` in `generate.py`, tried in order |
| The look | The colour and font tokens at the top of `template.html` |
| The edition times | `EDITIONS` in `generate.py` **and** the `cron` lines in the workflow |

The edition times live in two places because GitHub's scheduler runs in UTC. Finland switches between UTC+2 and UTC+3, so the workflow lists both sets of times and runs only the set that matches Helsinki's current offset. Runs start ten minutes before the hour because GitHub's scheduler is busiest on the hour. If you move to another time zone, change `TZ` in `generate.py` and the zone and offsets in the workflow's clock job.

The page is written in Finnish. To change the language, rewrite the language section of `SYSTEM_PROMPT`, the labels in `template.html`, and the Finnish day and month names in `generate.py`.

## Run it locally

```bash
pip install google-genai==3.3.0 feedparser==6.0.14
```

```bash
GEMINI_API_KEY=your-key python generate.py
```

Then open `index.html` in a browser.

## Cost and upkeep

- GitHub Actions and Pages are free for public repositories.
- Open-Meteo and the RSS feeds are free.
- Gemini costs depend on the model: about $7–15 a month for three Gemini 4 Carbon editions a day, nothing on the free tier.
- GitHub turns off scheduled workflows in repositories with no activity for 60 days. The daily commit of `index.html` keeps the schedule alive, so there is nothing to maintain.
