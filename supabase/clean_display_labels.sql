-- Clean display copy only. Keep consent, provenance, delivery records and reviews.
-- Run against the existing Text Monkey database, never against another project.
BEGIN;

UPDATE texty.events
SET title = btrim(regexp_replace(title,
  '^(\[(Synthetic|Fictional|Test dummy|Dummy|Fake|Mock)\][[:space:]]*|(Synthetic|Fictional|Test dummy|Dummy|Fake|Mock)[[:space:]]*[:,-]?[[:space:]]+)',
  '', 'i'))
WHERE title ~* '^(\[(Synthetic|Fictional|Test dummy|Dummy|Fake|Mock)\]|(Synthetic|Fictional|Test dummy|Dummy|Fake|Mock)[[:space:]])';

UPDATE texty.volunteers
SET preferences = jsonb_set(
  jsonb_set(preferences::jsonb, '{availability_note}',
    to_jsonb(regexp_replace(preferences->>'availability_note',
      '^Synthetic availability,[[:space:]]*', '', 'i'))),
  '{notes}', to_jsonb('Texting is disabled. Consent and qualifications must be verified.'::text))::json
WHERE preferences->>'synthetic' = 'true'
  AND preferences->>'availability_note' ~* '^Synthetic availability,'
  AND preferences->>'notes' = 'Fictional demo profile. No real consent or qualification evidence. Do not text.';

COMMIT;
