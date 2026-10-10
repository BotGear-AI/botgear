---
name: count-words
description: Count the words in each text file of a folder. Use when a task needs exact word counts.
---
# Count words

Run `scripts/count_words.py <folder>`. It prints JSON: `{"<file name>": <word count>, ...}`
for every `.txt` file in the folder. Use these numbers exactly; never count by hand.
