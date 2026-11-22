# Tyyni

A calm one-page broadsheet that writes itself. Weather and daylight for Espoo, the current beneath the day's material, a handful of signals, one reflection and one quiet closing line, set like printed paper. No links, no buttons, no archive, no state.

It comes in more than one edition, one per reader:

| Edition | Page | Content | Appears | Notification |
| --- | --- | --- | --- | --- |
| Main | **https://bjorkmeister.github.io/zen/** | Technology, economy, design, media, sport | 06.00 | GitHub issue thread |
| Minna | **https://bjorkmeister.github.io/zen/minna/** | Style, beauty, design, lifestyle, wellbeing, restaurants, culture, events in Helsinki and Espoo | 06.00 | An HTML email |

<p align="center">
  <img width="400" alt="An edition of Tyyni" src="https://github.com/user-attachments/assets/92652652-fe40-4539-972b-14fbdcf9d1a0" />
</p>

## How it works

At each edition hour (Helsinki time) a GitHub Action:

1. reads the weather and day length for Olari, Espoo from [Open-Meteo](https://open-meteo.com) (no key needed),
2. reads recent items from the edition's curated RSS feeds, and for Minna's edition upcoming concerts, exhibitions, theatre, dance and film from Helsinki's [Linked Events](https://api.hel.fi/linkedevents/v1/) API (no key needed),
3. asks Gemini to distil them into one calm page in Finnish,
4. renders `template.html` into the edition's `index.html`, commits it and publishes the site to GitHub Pages,
5. after the morning edition, sends the edition's notification.

If an edition fails, nothing is published for it and its previous edition stays up. GitHub emails you about the failed run, and the next look-in tries again.

| File | Role |
| --- | --- |
| `generate.py` | The editions' profiles; fetches the data, calls Gemini, renders the pages, sends the email |
| `template.html` | The page: typography, layout and placeholders |
| `email.html` | The morning email, for editions that notify by email |
| `.github/workflows/morning_edition.yml` | The look-ins, the commit, the deploy and the notifications |
| `.github/workflows/editor_settings.yml` | Saves the editor's desk settings from an issue |
| `toimitus/` | The editor's desk page, its settings and each edition's log |
| `index.html`, `minna/index.html` | The current editions, written by the workflow |

A story told in either edition is not offered again for two weeks: each run sets aside the links and titles the editor's log shows were chosen recently, and gives the writer the latest headlines to avoid.

Every edition is numbered (*N:o 1, N:o 2, …*). Each page records its own number in a hidden tag, and the next run adds one to it. Only published editions are counted, so a failed run doesn't skip a number. To start an edition's count again from N:o 1, delete its `index.html`.

## Run your own

You need a GitHub account and a Gemini API key. It takes about five minutes.

1. **Fork this repository.** Keep it public: GitHub Pages is free for public repositories.
2. **Get a Gemini API key** at [Google AI Studio](https://aistudio.google.com/apikey). The free tier works: the script falls back to Gemini 3.8 Flash. A paid key lets it use Gemini 4 Carbon, which writes better.
3. **Add the key as a secret.** In your fork, open *Settings → Secrets and variables → Actions → New repository secret*. Name it `GEMINI_API_KEY` and paste the key.
4. **Turn on Pages.** Open *Settings → Pages* and set *Source* to **GitHub Actions**.
5. **Turn on Issues.** Forks have them off by default. Open *Settings → General → Features* and tick **Issues**. The main edition's morning link is posted there.
6. **Turn on Actions.** Open the **Actions** tab and confirm that you want to run workflows in your fork.
7. **Run the first edition.** On the Actions tab, choose **Morning edition → Run workflow**. Leave *main* as the edition, or write `main minna` for both. It takes about two minutes.

Your page is now at `https://<your-username>.github.io/<repository-name>/`.

The first morning edition opens an issue called *Tyyni* that @-mentions you. Every morning edition adds a comment to that thread, so you get one email a day. If you don't receive it, check that email is enabled for *Participating and @mentions* in your GitHub notification settings.

### Send an edition by email

Editions whose profile says `notify="email"` (Minna's) arrive as an HTML email sent from a Gmail account. Gmail needs an *app password* for this: a separate 16-character password that can only send mail and can be revoked at any time.

1. Turn on 2-Step Verification for the Google account, if it isn't on yet.
2. Open [myaccount.google.com/apppasswords](https://myaccount.google.com/apppasswords), create an app password named *Tyyni*, and copy it.
3. Add three repository secrets:
   - `GMAIL_USER`: the Gmail address that sends the email.
   - `GMAIL_APP_PASSWORD`: the app password.
   - `MINNA_EMAIL`: the recipient. **Leave this out at first**: until it is set, the email goes to `GMAIL_USER` itself, so you can check how it looks.

Without the Gmail secrets the edition is still published; only the email is skipped.

### Make the timing punctual (recommended)

GitHub's own scheduler is best-effort. On a quiet repository it may run a schedule only every few hours, so editions arrive late or are skipped. A free external cron service fixes this by waking the workflow at the right minutes. Each wake-up only checks whether an edition is due, so an extra one costs nothing.

1. **Create a token.** On GitHub, open *Settings → Developer settings → Personal access tokens → Fine-grained tokens → Generate new token*.
   - *Repository access:* only this repository.
   - *Permissions:* **Contents: Read and write**. Nothing else.
   - *Expiration:* the longest available. Put a reminder in your calendar to renew it.
2. **Create a cron job** at [cron-job.org](https://cron-job.org) (free account):
   - *URL:* `https://api.github.com/repos/<your-username>/<repository-name>/dispatches`
   - *Schedule:* **Every day at 5:50**, time zone **Europe/Helsinki**. Check that *Next executions* lists the coming runs before saving.
   - *Backup (recommended):* clone the job (*Actions → Clone*) and set the copy to **Every day at 6:20**. If the edition is already out, the backup does nothing.
   - *Advanced → Request method:* **POST**
   - *Advanced → Headers:* `Authorization: Bearer <your token>` and `Accept: application/vnd.github+json`
   - *Advanced → Request body:* `{"event_type":"edition"}`
3. **Test it** with the job's *Test run* button. GitHub answers `204`, and a run appears on the Actions tab.

The workflow's own half-hourly schedule stays on as a fallback.

## The editor's desk

**https://bjorkmeister.github.io/zen/toimitus/** is a working page for steering the editions with data. It is not indexed by search engines, but it is public; only the repository owner can change anything.

- **Data from every edition** is kept in `toimitus/main.json` and `toimitus/minna.json` (the last 120 editions each):
  - which sources the writer saw and which it chose;
  - why it chose each item and how many others it weighed;
  - the model, time and tokens.
- **A critic:** a second, cheaper model (Gemini 3.8 Flash) reviews every edition on the same axes the knobs set. It also rates how well the claims are supported by the sources and how fresh the edition is, and lists any clichés.
- **Knobs:** tunne–fakta, vasen–oikea, huoli–toivo, paikallinen–globaali, tiivis–syvä and vakiintunut–heikot signaalit, from −2 to +2 for each edition. Each knob shows both the value set and the value the critic measured over the last seven editions, so you can see whether the writer follows. The political knob shifts emphasis only, never the facts.
- **Source weights:** boost a source (the writer is told to prefer it) or silence it (it is not read at all).

**Saving:** *Tallenna GitHubiin* opens a pre-filled issue titled *Toimitus: asetukset*; press *Submit new issue*. The *Editor's settings* workflow accepts it only from the repository owner, checks it strictly, saves it as `toimitus/asetukset.json` and closes the issue. The next edition uses the new settings.

## Make it yours

Each edition is a `Profile` in `generate.py`: its sources, its editorial voice, its section labels, its edition hours and how it notifies. To add a reader, add a profile to `PROFILES`, then add its page to the commit and staging steps in the workflow.

| To change | Edit |
| --- | --- |
| The location | `OLARI` (latitude and longitude) in `generate.py`, and the word *Espoo* in `template.html` |
| An edition's sources | `sections` in its profile: up to three RSS feeds per domain, or `events=True` |
| An edition's voice | `reader`, `principles`, `synthesis`, `signal_why` and `reflection` in its profile |
| An edition's hours | `editions` in its profile (and `TZ` for another time zone) |
| The language rules, weather line and closing line | `LANGUAGE`, `ATMOSPHERE` and `ZEN` in `generate.py` |
| The models | `MODELS` in `generate.py`, tried in order |
| The look | The colour and font tokens at the top of `template.html`, and the inline styles in `email.html` |

The workflow doesn't fire at exact times. At every look-in, `generate.py --due` (which knows Helsinki time, including summer time) lists the editions missing from their pages, and only those are written. A look-in that finds nothing due stops within seconds; one that finds an edition missing publishes it, so a lost run delays an edition instead of skipping it. Running the workflow by hand writes the editions you name, whatever the time; leave the field empty to redeploy the site without a new edition. A change to `toimitus/index.html` is deployed on its own.

The pages are written in Finnish. To change the language, rewrite `LANGUAGE` and the profiles' prompts, the labels in `template.html` and `email.html`, and the Finnish day and month names in `generate.py`.

## Run it locally

```bash
pip install google-genai==3.3.0 feedparser==6.0.14
```

```bash
GEMINI_API_KEY=your-key python generate.py --profile main
```

Then open `index.html` (or `minna/index.html` for `--profile minna`) in a browser.

## Cost and upkeep

- GitHub Actions and Pages are free for public repositories.
- Open-Meteo, Linked Events and the RSS feeds are free.
- Gemini costs depend on the model. With Gemini 4 Carbon, an edition costs roughly $0.10–0.25; on the free tier, nothing.
- GitHub turns off scheduled workflows in repositories with no activity for 60 days. The daily commits of the pages keep the schedule alive, so there is nothing to maintain.
