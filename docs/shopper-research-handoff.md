# Shopper research handoff

The application plan calls for **8–12 interviews** to choose one launch
category, followed by **30 real, consented shopping tasks** for evaluation.
No participant evidence has been supplied. Synthetic fixtures and public
product listings cannot stand in for participant judgments.

## Interview guide

Recruit Hungary-based shoppers who recently tried to find a garment or a
similar item. Use the same prompts for each interview:

1. Ask for a recent search. What started it: a photo, a remembered item, or
   words? What did the shopper actually try?
2. Which attributes mattered: garment type, colour, shape, size, brand,
   material, price, or retailer? What could change and still be useful?
3. What was the budget in HUF? Did shipping or availability change the choice?
4. Show two or three competing search flows using the shopper's own task.
   Where did each help or fail? Which result labels were understandable?
5. What would make the shopper trust a result's price, stock, image, and
   last-checked date? Would they open the retailer link?

Record category demand, common attributes, budget ranges, and failure reasons
as a short research summary. Select the first category from that evidence;
`jackets` in current examples is a working assumption, not a research result.

## Privacy and task records

Have the research owner approve the participant invitation, consent wording,
and private evidence store before recruiting. Store names, contact details,
raw queries, photos, and consent records outside Git under a private location.
`data/research/` is ignored by Git for local working files. Give each task a
pseudonymous ID and keep the link to the original evidence private.

For each of 30 tasks, run the text search against the same approved catalogue.
Record the ordered offer IDs, the options the participant judges useful and
current, and latency. If image matching has permitted images and an evaluated
index, run the photo flow on the same task and record its top five separately.
Do not infer usefulness from clicks or visual similarity alone.

The [task evaluation guide](task-evaluation-guide.md) defines the private JSON
format and command. Its tool checks the recorded IDs and current public
eligibility; it cannot verify consent or the participant's judgment.

## Decision record

Before changing a release gate, write down:

- Interview count, category decision, and why that category was selected.
- The 30-task denominator, successful task count, and failed task IDs.
- The source and date of each participant judgment and the private consent reference.
- Text versus photo top-five relevance and latency on the same tasks, if photo
  use is permitted. Keep photo search unavailable when this evidence is absent.

The release threshold remains 24 successful tasks out of exactly 30, each with
three current useful options from two approved sources. The repository can
calculate the count once real tasks and approved offers exist; it cannot create
those inputs.
