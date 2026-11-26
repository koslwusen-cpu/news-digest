# News digest

Your own news feed, from only the sources you choose, as articles, audio and video.

- **Email at 6:00, 12:00 and 18:00** (Paris time) with only what's new since the last one.
- **A web page** at `https://YOUR-USERNAME.github.io/news-digest/`, refreshed every hour, with the last 24h.
  Items you haven't been emailed yet are marked NEW.
- **On demand:** the page has two buttons: *Refresh this page* and *Email me now*.
  Each opens GitHub; tap **Run workflow** and it's done in about a minute. The GitHub phone app makes this quick.

Sources live in `feeds.yaml`. To add, remove or reorder them, edit that file; the comments at the top explain how.

## One-time setup (~15 min, free)

1. **Gmail app password.** Google Account → Security → 2-Step Verification → *App passwords*. Create one and copy the 16 characters.
2. **GitHub repo.** Create a new repository called `news-digest` and upload all these files, keeping the `.github` folder.
   Make it **public**: GitHub Pages is free only for public repos. Your password stays secret in step 3;
   the only public things are your list of sources and the page of headlines.
3. **Secrets.** Repo → Settings → Secrets and variables → Actions → *New repository secret*:
   - `GMAIL_USER`: your Gmail address
   - `GMAIL_APP_PASSWORD`: the app password
   - `DIGEST_TO` (optional): send to a different address
4. **Turn on the page.** Repo → Settings → Pages → Source: **GitHub Actions**.
5. **First run.** Actions tab → *News page* → Run workflow, then *Email digest* → Run workflow.
   You should get an email and a working page. Bookmark the page on your phone's home screen.

From then on it all runs by itself.

## Try it on your computer first (optional)
```
pip install -r requirements.txt
python digest.py preview      # writes preview.html with the real feeds; sends nothing
```

## Good to know
- GitHub sometimes starts scheduled jobs 10–30 min late. The 6:00 email might arrive at 6:20.
- If a source can't be reached, the email and page say so at the bottom. The rest still works.
- Reuters, AP and CNN have no working feeds of their own, so they come through Google News, filtered to their site.
- YouTube Shorts are skipped automatically.
