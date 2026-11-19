# Daily AI news brief

This is a small program that writes a short news brief every morning. It runs
by itself on GitHub and saves each brief in the `briefs` folder.

**Topic:** the AI industry.
**Reader:** a founder building an AI startup.

## What it does

Every day, the program:

1. **Reads four sources first:** Reuters, AP News, TechCrunch and The Verge
   (their AI sections).
2. **Keeps only new stories** from the last 24 hours.
3. **Picks the ten that matter most** to the reader. It leaves out opinion
   pieces, sponsored posts, unbacked rumours, gadget reviews and shopping
   deals. When several outlets cover the same story, it keeps one.
4. **Opens each article** and writes a one-line headline, two sentences on why
   it matters to a founder, and the link.
5. **Checks every claim against the article,** in two ways:
   - Every fact has to come with a quote copied from the article. The program
     itself (not the AI) looks for that quote in the article text. If the quote
     isn't there, the item is dropped.
   - A second, separate AI request reads the article and the item and looks for
     anything unsupported, overstated or wrong. If it finds anything, the item
     is dropped.

   An item is also dropped if it has no proper link (a full web address
   starting with `http://` or `https://`), if the article can't be opened (for
   example, a paywall), or if its publication date can't be confirmed.

   As a last safety check, the program looks at the finished brief before
   saving it. If any item has no link, it stops with an error and saves
   nothing. On GitHub, that run shows as failed on the Actions tab.
6. **Ends with one line** saying how many sources it read and how many items it
   dropped.

If items are dropped, the program moves down its shortlist to fill the ten
places. If it runs out of candidates that pass, the brief has fewer than ten
items. It never pads the brief with items it couldn't check.

## What you get

Two files per day in the `briefs` folder:

- `2026-11-19.md`: the brief.
- `2026-11-19.notes.md`: which items were dropped and why, plus any source that
  couldn't be read. Look here if a brief seems short.

A brief looks like this:

```
# News brief, 19 November 2026

1. **Example Labs raises $200M to build AI chips for data centres**
   Two sentences on why this matters to a founder.
   [Reuters](https://www.reuters.com/...)

...

Read 4 of 4 sources and opened 13 articles; dropped 3 items I could not check.
```

## Setting it up (one time)

You need an Anthropic API key. Get one at https://console.anthropic.com.

1. In this repository on GitHub, go to **Settings → Secrets and variables →
   Actions → New repository secret**.
2. Name it `ANTHROPIC_API_KEY`, paste your key as the value and save.
3. Merge this code into the repository's main branch. GitHub only runs
   scheduled jobs from the main branch.
4. Go to **Settings → Actions → General → Workflow permissions**. Choose **Read
   and write permissions** and save. This lets the job save each brief.

That's all. The brief is written every day at 05:17 UTC.

To get a brief right away, open the **Actions** tab, choose **Daily news
brief**, and click **Run workflow**.

## Changing what it covers

Everything you might want to change is in `config.toml`, which is written
in plain words:

- `topic`: what the brief is about.
- `reader`: who it's for. The "why it matters" lines are written for this
  person.
- `hours`: how far back to look (24 by default).
- `items`: how many items to include (10 by default).
- `ignore`: things to leave out. Add or remove lines.
- `sources`: the sites to read. Each one needs a name, a web address, and a
  `kind`: `"rss"` for a news feed, or `"page"` for a normal web page that lists
  articles.

To change the time it runs, edit the `cron` line in
`.github/workflows/daily-brief.yml`. `"17 5 * * *"` means 05:17 UTC every day.

## Running it on your own computer

You need Python 3.11 or newer.

```bash
pip install -r requirements.txt
```

```bash
export ANTHROPIC_API_KEY=your-key-here
```

```bash
python newsagent.py
```

The brief is printed on screen and saved in the `briefs` folder.

## Good to know

- **Cost.** Each run makes about 30 to 40 requests to Claude (model
  `claude-opus-5-5`), since every item is written and then checked separately.
  Check the price per run in your Anthropic console after the first few runs.
- **Some sites block automated readers.** Reuters and AP don't offer news
  feeds, so the program asks Claude to open their AI pages using Anthropic's
  web-fetch tool. If a site blocks it on a given day, the closing line says so
  (for example, "Read 3 of 4 sources") and the notes file shows the error.
- **If the AI declines a request,** the API automatically retries it on another
  Claude model. If that fails too, the item is dropped.
- **The checks catch made-up facts. They don't judge the story.** They confirm
  the brief says what the article says, not that the article itself is right.
  Wire services are listed first in the sources for that reason.

## Files

| File | What it is |
|---|---|
| `newsagent.py` | The program |
| `config.toml` | Your settings: topic, reader, sources, what to leave out |
| `requirements.txt` | The Python packages it needs |
| `.github/workflows/daily-brief.yml` | The daily schedule on GitHub |
| `briefs/` | Where the briefs are saved (created on the first run) |
