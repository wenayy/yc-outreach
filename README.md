# YC Outreach

Pick any Y Combinator batch, get every company's founders and their likely email addresses, and write a personalised
cold email to each one from a single template. Review up to three contacts per company and automatically send messages
in shared two-minute batches across connected SMTP mailboxes when running locally. Drafting is free without API keys;
automatic sending requires mailbox verification through your own Apify account.

```
python3 serve.py        # open http://localhost:8765
```

Python 3.9+, standard library only. Nothing to install.

## What it does

1. **Pick a batch** (Summer 2005 through the latest) from YC's public directory.
2. **Founders load 20 companies at a time** (about 5–10 s per 20); click **Load more** for the next 20. For each
   company the server reads its ycombinator.com page for the founders' names, titles, LinkedIn and X. Loaded
   companies are kept in your browser, so reopening a batch is instant.
3. **Emails are filled in** for each founder, best source first:

   | Label | Source | Reliability |
   |---|---|---|
   | `mailbox verified` | Recent SMTP mailbox and catch-all check | Positive mailbox response; still no delivery guarantee |
   | `checked` | Apify format and mail-domain check with your token (optional) | Address format and domain passed; mailbox is not guaranteed |
   | `on site` | Email published on the company's homepage, `/contact`, `/team` or `/about` page | Real address, may be generic |
   | `guess` | `first@domain`, then `first.last@`, `flast@`, `firstlast@` | A possible company-address pattern; review before queuing |

4. **Write once, send many.** Fill in your name and links, edit the subject and body, and every draft updates live.
   Copy the text or open it in your mail app. When running locally, review the drafts and add them to the automatic sending queue. Mark companies as sent to hide them.

Your details, template, "sent" marks and loaded batches are stored in your browser (`localStorage`). Automatic sending stores reviewed drafts and delivery status locally in `.outreach/queue.sqlite3`. SMTP credentials are read from your environment or `.env`, never from browser storage.

## Two-template rotation

**Allow catch-all recipients to send after verified mailboxes** is an explicit opt-in in step 5. It accepts recent
catch-all evidence while keeping invalid, risky, disposable, unknown, suppressed and stale addresses out of sending.
Verified recipients are sent before catch-all recipients. The setting persists across restarts and can be switched off
to return remaining catch-all drafts to hold. Catch-all mailboxes are not labeled verified and may still bounce.

Write Template A, enable **Alternate templates A and B for each sender**, and write Template B's subject and body.
Both use the same personalization fields. Review saves both personalized drafts for each recipient; you can edit
either before queueing. The worker selects A, B, A, B separately for each actual sending address across attempts.
Its sequence persists across restarts. Held and cancelled drafts do not advance the sequence; failed or uncertain
attempts do. Sender changes use the actual sender's sequence. Sent history shows the template used.
Previously queued single-template messages retain their saved wording; rotation applies to newly reviewed drafts.
Different wording can make messages more relevant, but does not guarantee inbox placement or reduce spam classification.

## Apify address tools

New lookups skip companies marked sent and recipients already sent, bounced, or with an uncertain/in-progress send.
The local app reloads sending history immediately before each lookup and stops if that history cannot be read.
The domain-check confirmation displays how many new addresses will be checked and how many already contacted addresses were skipped.
This applies to domain screening, mailbox verification, and company contact discovery even when an old batch is loaded again.
Mailbox verification still includes explicit held/pending drafts at a previously contacted company; completed recipients are never rechecked.
Resuming a saved Apify run imports its existing results rather than paying to restart that run.

**Check domains** restores the original
[`vulnv/email-validator`](https://apify.com/vulnv/email-validator) in batches of up to 50 loaded candidate addresses.
It screens address format and whether the domain can receive mail, then labels passing addresses **checked (domain)**.
It does not find new contacts, prove a specific mailbox exists, or release held messages for sending.
Use it as optional screening before **Verify mailboxes**. Each run uses the displayed budget cap and can resume after an interruption.

The in-app **Verify mailboxes** button calls
[`bounceverify/bounceverify-email-verifier`](https://apify.com/bounceverify/bounceverify-email-verifier), with SMTP and catch-all
results required, in batches of up to 50 addresses. It includes the currently loaded contacts and existing held drafts,
so you can validate the saved queue without reloading the same YC batch. Old `checked` labels only mean that a
format/domain check passed; they do not satisfy the mailbox gate.

Start with **Test up to 10 mailboxes** to inspect a small sample before a full check. The sample includes fresh
candidates, your configured sender addresses, and up to three known hard bounces when available. It is the only
lookup that intentionally rechecks known bounced addresses as controls. Sample results are shown individually;
they do not authorize queue drafts. A known hard bounce marked deliverable triggers a warning. Review unknown
results and the controls before proceeding; this integration has not demonstrated a lower bounce rate on your list.
Both sample and full checks require a cost confirmation, use separate saved runs, and send no test emails.
**Refresh saved results** reimports recent completed BounceVerify evidence recorded in the local queue without
starting or charging for a new verification run. Other providers and unavailable runs are skipped. This is useful
after a result-parser update, including providers that return `risk_type: "none"` to indicate no risk.

Only a completed run with `status: valid`, `smtp_valid: true`, explicit non-catch-all/non-disposable results,
positive syntax/domain/MX signals and no risk/error can release a draft. Locally, the server verifies the actor identity
and requested addresses, then reads the completed run directly from Apify using your token
temporarily; it stores only address verdicts, timestamps and the run ID. The token is never saved. Mailbox evidence
expires after seven days and is checked again for freshness before sending. Unknown, risky and catch-all results stay
on hold; invalid addresses are excluded. A positive result reduces bounce risk but does not guarantee delivery.
Previously imported evidence from the old SMTP actor remains subject to the same seven-day expiry; old provider
runs are never resumed automatically by the new button. The server retains explicit support for importing old SMTP
evidence, but it accepts only either of the two fixed verifier actors, never an arbitrary actor.

**Find more company contacts** calls
[`code_crafter/leads-finder`](https://apify.com/code_crafter/leads-finder) for up to 33 loaded company domains, with an explicit `fetch_count: 100` cap. It requests validated work-email leads for founders, CEOs, CTOs, engineering managers and recruiting contacts. Named results must match a requested company and its email domain; duplicates and already contacted or suppressed recipients are excluded. Up to three contacts per company are kept as drafts for review, not proof of mailbox verification. Previously searched companies are skipped for 24 hours. The budget field caps each run; no contacts or results are guaranteed.

Lookups run on your Apify account after a cost confirmation. The UI requests an abort after eight minutes. A saved run
ID lets you resume/import after a page refresh or connection failure without starting that run again. Provider failures,
quota errors or missing results never authorize sending.

If you want to enrich the list further, use these tools separately in Apify, then review the returned addresses before
adding them to the queue:

| Need | Apify option | What it provides |
|---|---|---|
| A better candidate for a named YC founder | [`scrapersdelight/work-email-finder-scraper`](https://apify.com/scrapersdelight/work-email-finder-scraper) | Name plus company domain, candidate addresses, confidence, and public-source evidence. It does not perform SMTP mailbox verification. |
| More relevant people at one company (integrated) | [`code_crafter/leads-finder`](https://apify.com/code_crafter/leads-finder) | Used by **Find more company contacts**. The app independently verifies its candidate mailboxes before sending. |
| A second paid comparison | [`nexgendata/person-business-email-finder`](https://apify.com/nexgendata/person-business-email-finder) | Name plus company-domain lookup. It is substantially more expensive than the named-founder option above, so use it only when you need a comparison. |

Do not use the old `snipercoder/email-finder-by-name-and-domain` actor for new work. It is currently marked as under
maintenance in the Apify Store and its runs can stall or return no results.

## Automatic sending (local)

1. Copy `.env.example` to `.env` and enter your provider's SMTP host, username and app password. Use `SMTP_SECURITY=starttls` with port 587, or `ssl` with port 465, according to your provider. Keep `.env` private; it is excluded from Git and Vercel uploads.
2. Run `python3 serve.py`, load companies and finish your email template.
3. Optionally use **Find more company contacts** when founder addresses are missing or invalid. Click **Review loaded companies** to preselect up to three distinct contacts per company. Each gets a separate draft with the correct greeting. Invalid and blocked addresses are excluded; earlier sent and queued contacts count toward the three-contact limit. The compact cards show the recipient and planned sender. Uncheck individual drafts or turn off **include suggested addresses** to exclude guesses.
4. Click **Queue selected**. Duplicate recipients are skipped across all mailboxes. Unverified drafts are saved as `held`, including older pending messages after upgrading. Click **Verify mailboxes** with your Apify token to release only addresses with acceptable evidence. Editing the recipient requires verification of the new address as well.
5. Select the ready sender accounts, set the batch interval (default two minutes, adjustable to 60 minutes), then click **Start sending**. Verified/checked-source messages go first, then public/manual sources, then guesses that subsequently passed mailbox verification. Each batch uses one priority tier. The daily total can be up to 200 across selected mailboxes, even with one sender. Up to 1,000 drafts can be queued per request; the queue can span multiple days. Apify's 50-address chunks are processing batches, not a daily verification cap. Provider sending limits still apply.

Additional Gmail accounts use `SMTP_2_USER` / `SMTP_2_PASSWORD` and `SMTP_3_USER` / `SMTP_3_PASSWORD` in `.env`. Each needs its own Google app password. Additional accounts default to Gmail SMTP; optional `SMTP_2_HOST`, `SMTP_2_PORT`, `SMTP_2_SECURITY`, `SMTP_2_FROM`, and `SMTP_2_FROM_NAME` (and corresponding `SMTP_3_` keys) support other SMTP settings. The UI shows account readiness without exposing passwords. Each selected account sends one different queued message in a shared two-minute batch by default. With three ready accounts, a worker pass sends up to three distinct messages, one through each account, then waits two minutes before the next batch. If an account sent recently, the batch waits until every participating account reaches its interval. The queue records the planned sender before sending and the actual sender after acceptance. Duplicate-recipient protection and blocking apply across all accounts. A sending error pauses the whole queue; another account does not retry that recipient.

For a macOS background service that runs after closing Terminal and starts at login:

```sh
python3 background_service.py install
python3 background_service.py status
# After changing app passwords:
python3 background_service.py restart
# To stop it:
python3 background_service.py stop
```

The installer writes `~/Library/LaunchAgents/com.ycoutreach.local.plist`; its private runtime, shared queue and logs live under `~/Library/Application Support/YCOutreach`. Edit the project `.env`, then run `python3 background_service.py restart` to refresh the runtime and credentials. The project `.outreach/service-data-path` points manual runs to the same queue. Keep this project in the same location. Open `http://localhost:8765` to manage the queue. The service starts with sending paused after each restart. It does not send while your Mac is asleep, shut down, or logged out, and it does not burst missed messages on waking. Do not run `serve.py` manually at the same time as the service.

Resend's free plan currently includes a 100-email daily limit, but its [acceptable-use policy](https://resend.com/legal/acceptable-use) prohibits cold outreach and scraped contact lists and requires opt-in. This cold-outreach queue therefore uses your SMTP mailboxes, not Resend. Adding accounts or reducing intervals does not guarantee inbox placement.

Keep the Python server running and your computer awake. Sending continues when you close the browser. Queued messages are snapshots: changing the main template does not change already queued drafts. **Cancel pending** removes them from the sending schedule so you can review and queue new versions. **Pause** stops new sends; an email already being sent may finish first.

The queue persists across restarts and always starts paused. Duplicate recipients are skipped. SMTP failures pause the queue and are never retried automatically. A crash during sending is labeled `uncertain`; inspect your mailbox before contacting that person again. `sent` means the SMTP server accepted the message, not that it reached the inbox.

History has **Queue**, **Sent**, **Bounced / failed**, and **All** tabs, a search field, 20 rows per page and a bounded
scroll area. Each recipient retains a colored dot for its original address source; hover for the source and current
mailbox verdict. Earlier sends remain historical records and are not relabeled as mailbox-verified.

Click **Check Gmail bounces** to read the last 14 days of delivery reports from the connected Gmail inboxes, up to 100
reports per account. The scan is read-only and looks for structured delivery-status notifications with a permanent
failure; it only updates recipients sent through the corresponding mailbox. A hard bounce becomes `bounced` and the
address is blocked across all accounts. For a report the scan cannot read, enter the failed address and click **Mark
bounced**. Scanning uses the existing Gmail app passwords; IMAP access must be available. It does not monitor replies,
spam/trash folders or delivery reports continuously. Use **Block address** for opt-outs and other known bad addresses.

Spacing is volume control, not an inbox guarantee. Authenticate your sending domain with SPF, DKIM and DMARC, keep messages relevant, and respect opt-outs. See [Gmail's sender guidelines](https://support.google.com/mail/answer/81126). Apify verification and public listings can also become outdated; this app cannot guarantee a mailbox is correct.

The sending API is local-only and requires a session token for changes. The local server only serves the page and API endpoints, so `.env` and the queue database cannot be downloaded through it.

```sh
python3 -m unittest discover -s tests -v
```

Tests use a fake clock and mocked SMTP; no actual emails are sent.

## Deploy

Import the repo on [Vercel](https://vercel.com/new). No build step, no environment variables. `index.html` is served
as a static page and `api/yc.py` runs as a Python serverless function (`vercel.json` gives it 60 s).

Automatic sending is currently available only through `serve.py`. Vercel does not run the local SMTP worker or persist its SQLite queue. Hosted automatic sending requires a durable database, authenticated sender access and an external scheduler; this repo does not configure those services.

## Project layout

| File | Role |
|---|---|
| `index.html` | The whole UI: one HTML file with inline CSS and JS, no framework, no build. |
| `api/yc.py` | Serverless function (Vercel Python runtime, `handler` class). YC search + founder pages. |
| `serve.py` | Local web server, `/api/yc` routes and protected `/api/mail` queue controls. |
| `outreach.py` | Persistent SQLite queue, SMTP transport and configurable sending worker. |
| `contact_emails.py` | Public-address extraction and conservative founder matching. |
| `mailbox_checks.py` | Apify mailbox-evidence import and conservative Gmail hard-bounce parsing. |
| `vercel.json` | Function timeout. |
| `yc_scraper.py` | CLI: scrape whole batches to JSON/CSV. |
| `apify_enrich.py` | Legacy CLI enrichment script. It still targets the old SniperCoder actor; do not use it until that actor is replaced and retested. |

## HTTP API

The YC lookup endpoints below are `GET` and return JSON. Locally, `/api/mail` also supports `GET` for queue status and token-protected `POST` actions (`enqueue`, `start`, `pause`, `cancel`, `suppress`). Errors return `{"error": "..."}` with status 400 (bad input) or 502 (YC
unreachable).

### `/api/yc?action=batches`

```json
[{"batch": "Fall 2026", "count": 110}, {"batch": "Summer 2026", "count": 231}]
```

Newest first. Includes `"Unspecified"`, which the UI hides.

### `/api/yc?action=companies&batch=Winter%202024`

```json
[{"name": "Indemni", "slug": "indemni", "batch": "Winter 2024", "website": "http://www.indemni.com",
  "one_liner": "Cargo Theft and Fraud Prevention Platform", "industry": "B2B -> Supply Chain and Logistics",
  "team_size": 7, "launched_at": 1708029636}]
```

`batch` must look like `Winter 2024` (season + year).

### `/api/yc?action=founders&slugs=indemni,parcelbio`

At most 10 slugs per call (`^[a-z0-9-]+$`). The UI loads 20 companies per click as two parallel calls. About
5–9 s per call; each company's website check is cut off after 4 s (`SITE_DEADLINE`) so one slow site can't stall
the batch.

```json
[{"slug": "indemni", "website": "http://www.indemni.com", "domain": "indemni.com",
  "linkedin": "https://www.linkedin.com/company/...", "twitter": "", "site_emails": [],
  "founders": [{"name": "Omar Draz", "title": "Founder", "linkedin": "https://linkedin.com/in/odraz",
                "twitter": "https://twitter.com/oamdraz", "emails_found": [],
                "email_guesses": ["omar@indemni.com", "omar.draz@indemni.com", "odraz@indemni.com", "omardraz@indemni.com"]}]}]
```

A company whose YC page fails to load comes back as `{"slug": "...", "error": "..."}`. Guesses are empty when the
domain doesn't resolve.

## Where the data comes from

- **Batches and companies:** YC's public company search (Algolia). The read-only search key is read from
  `ycombinator.com/companies` at runtime, so no key is stored here.
- **Founders:** the `data-page` JSON embedded in each `ycombinator.com/companies/<slug>` page. Browsers can't fetch
  these cross-origin, which is why this part runs on a server.
- **Mailbox checks:** [`bounceverify/bounceverify-email-verifier`](https://apify.com/bounceverify/bounceverify-email-verifier). Use the sample button before a full check.
  Runs start in the browser; locally the server imports the evidence with a transient token and checks it before sending.
- **Additional contacts:** [`code_crafter/leads-finder`](https://apify.com/code_crafter/leads-finder),
  merged with known founders as separate named contacts rather than CC recipients.

## Command line

For bulk exports. Output files are gitignored.

```
python3 yc_scraper.py --batches "Winter 2024" "Summer 2024" --out yc_founders   # -> yc_founders.json + .csv
```

`yc_scraper.py` flags: `--batches` (default `"Summer 2026" "Fall 2026"`), `--out`, `--workers` (default 12),
`--limit` (first N companies, for testing). It checks six contact pages per site and retries, so it's slower but
more thorough than the web API.

`apify_enrich.py` reads `APIFY_TOKEN` from the environment or a `.env` file (gitignored), but it is a legacy script
because it targets the stalled SniperCoder actor. Use the in-app **Verify mailboxes** and **Find more company contacts**
buttons for the current workflow.

## Notes for AI agents and contributors

- Standard library only, on purpose. Don't add dependencies or a build step.
- `api/yc.py` and `yc_scraper.py` share logic but are separate on purpose: the function has to finish inside a
  serverless timeout, so it makes one attempt per fetch, checks 4 pages in parallel, and gives up on a website after 4 s;
  the CLI retries and checks 6.
- The function only accepts a batch name or slugs. Websites always come from YC's data, never from the request, so
  it can't be used to fetch arbitrary URLs. Keep it that way.
- Everything from YC is untrusted text. `index.html` escapes it (`esc()`) and only links `http(s)` URLs (`url()`).
- To test locally: `python3 serve.py`, then
  `curl 'localhost:8765/api/yc?action=founders&slugs=reddit'`.

## Be decent

Write to people one at a time, keep it short, and take "no" for an answer.

## License

MIT

Unverified sending is an explicit opt-in in step 5. It releases guesses and inconclusive or unchecked addresses after verified mailboxes and catch-all recipients. Known invalid, risky, disposable, bounced and blocked addresses remain excluded. This can increase bounces. The daily total supports up to 500 across selected senders.
