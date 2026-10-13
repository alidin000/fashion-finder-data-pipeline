# Partner access handoff

This is an operator checklist for the two [feed-only candidates](hungarian-source-candidates.md):
ABOUT YOU Hungary and Answear Hungary. Their public program listings are leads,
not feed credentials or permission to republish products. No partner approval
or usable feed has been supplied to this repository.

## Ask each program

Use the publisher application or partner contact shown in the program account.
The message below is a draft for the account owner to review and send; the
repository does not send it.

> Subject: Hungary product-feed access and usage terms for Fashion Finder
>
> Hello, we are preparing a small Hungary-focused clothing search pilot. We
> would like to understand whether our publisher account may use your Hungarian
> product feed for a comparison/search experience. Please confirm the approved
> website or app, feed format and download method, update frequency, direct or
> affiliate link rules, required attribution, and whether we may display product
> titles, HUF prices, availability, and images. Please address image display,
> image processing for similarity search, temporary caching, and retention
> separately. If any use is restricted, please tell us the allowed alternative.
> We can share a short description of the pilot and its intended audience.

Record each answer privately under `data/partner/<source>/`. Save the program
terms, approval record, account/website scope, feed sample, field mapping,
and any image or deep-link restrictions. Never commit tokens, feed URLs, raw
exports, agreements, or contact details. `data/partner/` is ignored by Git.

## Record one source decision

For **each** source, have a human reviewer fill this table in a private note:

| Decision | Evidence to record |
| --- | --- |
| Publisher and approved website/app | Account and program approval reference, date, and allowed domain. |
| Product data | Feed access method, Hungarian market scope, permitted fields, and refresh interval. |
| Links and attribution | Direct/affiliate link rule, tracking requirement, visible attribution, and expiry. |
| Images | Display, processing for similarity search, temporary caching, and retention permissions as separate answers. |
| Operations | Contact for source failures, rate or download limits, and revocation process. |

Do not set `approval_status: "approved"` based on a public listing alone.
Set `allow_public_listing`, `allow_image_display`, and
`allow_image_processing` only for uses the recorded terms support. The source
policy's `evidence_ref` should point to the private record, without copying
the agreement or credentials into the repository.

## After approval and a sample arrive

1. Inspect the actual XML tags, Hungarian product links, price and currency,
   observation time, image links, categories, and variants. Write a source-specific
   JSON field map under `data/partner/<source>/`.
2. Run `fashion-finder map-xml-feed` to a **staging** CSV outside `data/feeds/`.
   Run `fashion-finder validate-feed` on that CSV. See the
   [mapping guide](hungarian-source-candidates.md) for exact syntax.
3. Manually compare at least ten sample rows with their current Hungarian
   product pages and partner rules. A valid CSV does not prove a current offer.
4. Record the policy decision. Deliver the reviewed CSV atomically as
   `data/feeds/<source>.csv`; inspect the worker report and public API.
5. Use `readiness`, `audit-sample`, and `audit-report` for the selected category.
   The [local Docker runbook](local-docker-runbook.md) has the worker commands.

If access is denied or the terms exclude the intended use, leave the policy
pending and choose another source. The app must not silently treat an accessible
website as an approved feed.
