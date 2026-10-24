# Your hunt in a Google Sheet

Snipe Hunt can write what it knows to a Google Sheet you own after every
hourly run. It only writes, and it never reads anything back: changing the sheet
changes nothing in the bot.

| Tab | What is in it | Rewritten each push? |
|---|---|---|
| Summary | Cards kept, what you paid, spending by month (with a chart), live picks, offers, lowball targets, Gixen totals, the median sold price | No: written once as formulas, then yours to edit. Delete it and the next push writes a fresh one. |
| Collection | `inventory.csv` | Yes |
| Picks | The live picks: tier, stars, price, max, going rate, end time | Yes |
| Sold | Every sold price the bot has recorded, newest first | Yes |
| Offers | Best Offer listings up 30+ days, with an amount to offer | Yes |
| Lowball | Cheap auctions of good cards, each with one low snipe | Yes |
| Snipes | Your scheduled Gixen snipes, with the bot's verdict and any doubts | Yes |
| Data Sources | Where each tab came from, when, and how many rows | Yes |

Any other tab you add is left alone.

## Setup (about five minutes)

The bot runs on its own, so it signs in to Google as a *service account*: a
robot account that can only touch sheets you share with it.

1. Go to [console.cloud.google.com](https://console.cloud.google.com), create a
   project (any name, for example "snipe-hunt").
2. **APIs & Services → Library**: search for **Google Sheets API** and click
   **Enable**.
3. **APIs & Services → Credentials → Create credentials → Service account**.
   Give it a name, skip the optional steps.
4. Open the new service account, **Keys → Add key → Create new key → JSON**.
   A `.json` file downloads. Keep it private: it is the account's password.
5. Copy that file next to the bot, for example to `home/google-key.json` (the
   Docker setup mounts `home/` as `/var/lib/snipe-hunt`), and make it readable
   only by you (`chmod 600`).
6. Create a Google Sheet (an empty one is fine). Click **Share** and add the
   service account's email (it looks like
   `name@project.iam.gserviceaccount.com`, and is in the JSON file as
   `client_email`) as an **Editor**.
7. In `.env`:

   ```
   GOOGLE_SHEET_ID=<the long id between /d/ and /edit in the sheet's link>
   GOOGLE_SERVICE_ACCOUNT_FILE=/var/lib/snipe-hunt/google-key.json
   ```

8. Restart the bot, then push once by hand to check:

   ```
   docker compose exec snipe-hunt python -m snipehunt sheets
   ```

   It prints how many rows went to each tab. "Google Sheets 403" or "404"
   means the sheet isn't shared with the service account's email, or the id is
   wrong.

## Notes

- Values are written as plain values, never as formulas, so a listing title that
  starts with `=` stays text.
- Times are in the bot's time zone (`SNIPEHUNT_TZ`) as text like
  `2026-10-25 21:30`, which sorts correctly.
- Money columns hold numbers; an empty cell means the bot doesn't know (for
  example, shipping not stated).
- The Summary's spending table reads the Collection tab's **Paid ($)** and
  **Order date** columns. If you reorder Collection's columns yourself, the next
  push puts them back, so add your own columns on a tab of your own instead.
- A failed push is logged and the hunt carries on.
