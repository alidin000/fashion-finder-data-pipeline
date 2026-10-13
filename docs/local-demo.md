# Run the synthetic local demo

This demo lets you test search, category, colour, size, and HUF budget in the
real Django API and Next.js page. Its 12 products and prices are invented. Five
entries display [CC0 sample images](demo-image-credits.md) downloaded from
Wikimedia Commons. It does not collect retailer data, contain retailer links,
or verify stock. It is **not** private-beta evidence.

From the repository root with Docker Compose available:

```bash
bash scripts/start_local_demo.sh
curl -fsS http://127.0.0.1:8300/api/v1/health
curl -fsS 'http://127.0.0.1:8300/api/v1/search?category=jackets&max_price=15000'
```

Open **http://127.0.0.1:3300** and try `black jacket under 15000 Ft`.
For photo search, download one of the images linked in the
[credits](demo-image-credits.md), upload it, and choose **Find similar styles**.
The same bundled image should rank first. Category, colour, size, and HUF
budget filters apply to the photo results. Ranking uses coarse colour and
layout features; it does not recognize garment identity or general fashion
similarity reliably. No photo is stored by the server.
The page displays a **LOCAL DEMO** banner. Sample cards have no retailer link.
The source policy and SQLite database live under ignored `data/demo/` by
default. The script uses Docker project `fashion-finder-demo`, image tags
`fashion-finder-api:demo` and `fashion-finder-web:demo`, and ports 8300/3300;
it leaves the normal local stack on ports 8000/3000 and its data untouched.

The seed command refuses an existing database without its demo marker and
refuses to replace a different policy file. It requires
`FASHION_FINDER_DEMO_MODE=1`. Re-running the script refreshes the sample
timestamps without duplicating offers and rebuilds the local image index. The public API hides demo offers when
the flag is absent. Release-readiness, manual-audit, and shopper-task gates
exclude them even when the flag is on.

To stop the demo containers while keeping its database:

```bash
docker compose -f compose.yaml -f compose.demo.yaml down
```

The script accepts `FASHION_FINDER_DEMO_DATA_DIR`,
`FASHION_FINDER_DEMO_API_PORT`, and `FASHION_FINDER_DEMO_WEB_PORT` to change
the isolated data path or host ports. The normal [local Docker runbook](local-docker-runbook.md)
describes the real-feed stack and its pending source policy.
