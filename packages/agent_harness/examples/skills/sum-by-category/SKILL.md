---
name: sum-by-category
description: Total the amounts in an expenses CSV (date,category,amount), per category and overall. Use when a task needs exact spending totals.
---
# Sum by category

Run `scripts/sum_by_category.py <csv file>`. It prints JSON:
`{"by_category": {"<category>": <total>, ...}, "total": <grand total>}`, rounded to cents.
Use these numbers exactly; never add amounts up by hand.
