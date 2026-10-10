---
name: word-report
description: Report how long each document in a folder is.
inputs:
  docs: {type: path, required: true, description: a folder of .txt files}
skills: [count-words]
outputs:
  - output/counts.json
  - output/report.md
checks:
  - "{python} {workflow_dir}/checks/check_counts.py --docs inputs/docs --counts output/counts.json"
policy:
  network: deny
  max_minutes: 5
---
Use the `count-words` skill to count the words in every document in `inputs/docs`.

Write `output/counts.json` exactly as the skill's script prints it, then write
`output/report.md`: one line per document, longest first, with its word count.
