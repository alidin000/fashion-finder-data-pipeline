# Private beta release handoff

This page lists the remaining evidence for the
[technical application plan](application-implementation-plan.md). It is a
checklist, not a claim that the private beta is ready. Run commands from the
repository root against a **copy** of the approved local catalogue first.
The [synthetic local demo](local-demo.md) is excluded from these gates.

| Gate | Current state | Evidence needed |
| --- | --- | --- |
| Category choice | Open | 8–12 real shopper interviews and one recorded category decision. See [research handoff](shopper-research-handoff.md). |
| Two source permissions | Open | Approval and terms for two Hungarian sources, including listing, links, attribution, and each image use. See [partner handoff](partner-access-handoff.md). |
| Catalogue coverage | Open | At least 100 current, public-eligible offers per source in the chosen category. |
| Manual audit | Open | 30 current reviewed offers per source, with correct HUF price, category, Hungarian link, and truthful availability/size. |
| Shopper tasks | Open | Exactly 30 consented tasks; at least 24 with three useful current options across two approved shops. |
| Photo search | Open | Permitted images, a versioned index, and a same-task relevance comparison. Keep the capability unavailable until measured. |
| Live smoke | Open | An approved feed imported in the deployed environment, a working API and browser flow, and one opened retailer link. Record command, location, and result. |
| Private beta | Open | 10–20 invited participants, a named source-failure owner, monitoring, and a rollback rehearsal. |

The existing local commands produce **recorded evidence**, not permission:

```bash
fashion-finder --data-dir data readiness --category jackets --policy data/source-policies.json
fashion-finder --data-dir data audit-sample --category jackets --source aboutyou --policy data/source-policies.json --output data/audits/aboutyou-jackets.csv
fashion-finder --data-dir data audit-report data/audits/aboutyou-jackets.csv data/audits/answear-jackets.csv --category jackets --policy data/source-policies.json
fashion-finder --data-dir data evaluate-tasks data/evaluation/jackets.json --category jackets --policy data/source-policies.json
```

Replace `jackets`, source names, and file paths after the category and sources
are approved. Keep completed audit files, task labels, and evidence private.
The `readiness` command reports declared policy and current stored coverage;
the audit and task commands check submitted human labels. None inspects a
publisher agreement, participant consent, or a live retailer page.

The [local Docker runbook](local-docker-runbook.md) covers deployment, health,
backup, and worker checks. Its synthetic smoke test proves the software path,
not a live retailer integration. Do not mark any gate complete from synthetic
or assumed evidence.
