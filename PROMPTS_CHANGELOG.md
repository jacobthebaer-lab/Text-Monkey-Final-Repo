# Prompts changelog

Every change to a file in `/prompts` gets an entry here: which prompt, old →
new version, what was wrong, and what changed (including the eval failure or
observation that prompted it).

## parser.md

- **v1** (2026-09-29): Initial version. Strict-JSON intent classifier with a
  sensitive flag and confidence score; explicitly forbidden from replying or
  counseling. Written against the sample texts in `data/sample_texts.json`.
