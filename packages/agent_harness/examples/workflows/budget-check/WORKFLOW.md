---
name: budget-check
description: Decide whether a month's expenses stayed within a budget, and say why.
inputs:
  expenses: {type: path, required: true, description: a CSV file with date,category,amount}
  budget: {type: string, required: true, description: the budget for the period, e.g. 400}
skills: [sum-by-category]
outputs:
  - output/totals.json
  - output/verdict.md
deliverables:
  - output/verdict.md              # what a person reads; totals.json is the evidence
checks:
  - "{python} {workflow_dir}/checks/check_verdict.py --expenses inputs/expenses --budget {input.budget} --totals output/totals.json --verdict output/verdict.md"
policy:
  network: deny
  max_minutes: 5
# For agents that can't run tools (prompt_inject): the harness runs this and
# puts its output in the prompt. Tool-using agents run the script themselves.
context_commands:
  - title: Totals (sum_by_category.py)
    command: "{python} .claude/skills/sum-by-category/scripts/sum_by_category.py inputs/expenses"
---
Use the `sum-by-category` skill on `inputs/expenses`, then compare the total
with the budget ({input.budget}).

1. Write `output/totals.json` exactly as the skill's script prints it.
2. Write `output/verdict.md`. Its first line is exactly `OVER BUDGET` or
   `WITHIN BUDGET`. Then one short paragraph: the total, the budget, the
   difference, and the largest category.
