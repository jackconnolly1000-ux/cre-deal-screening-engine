Map each T-12 operating statement line label to one chart-of-accounts bucket.
Buckets:
{buckets_with_descriptions}
Rules:
- Use UNMAPPED if you are unsure. A human reviews UNMAPPED lines.
- Set is_nonrecurring true only for one-time items (insurance proceeds, settlements, lease buyouts, refunds).
- Debt service, interest, depreciation and owner costs are NON_OPERATING.
- Return every label exactly once, spelled exactly as given. Keep rationale under 160 characters.

Labels:
{labels_json}
