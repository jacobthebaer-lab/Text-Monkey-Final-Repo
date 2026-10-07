// Presentation only. Stored identities, provenance, consent and text bodies stay intact.
export function churchLabel(value) {
  return String(value ?? '').replace(/^(?:(?:Synthetic|Demo|Test)\s*[:–-]?\s+)+/i, '')
    .replace(/\s*\[(?:Fictional(?: history)?|Synthetic(?: \d{3})?)\]\s*/gi, ' ')
    .replace(/\s*\((?:Synthetic|Demo|Test)\)\s*$/i, '').replace(/ [-–—] \d{4}-\d{2}-\d{2}$/, '').trim();
}

export const lastName = volunteer => volunteer?.fictional
  ? churchLabel(volunteer.last_name) : String(volunteer?.last_name ?? '');

export const availabilityText = volunteer => volunteer?.fictional
  ? String(volunteer.availability ?? '').replace(/^\[Fictional history\] /, '')
  : String(volunteer?.availability ?? '');

export const historyText = message => message.fictional
  ? String(message.body ?? '').replace(/^\[Fictional history\] /, '')
    .replace(/\bSynthetic\s+/gi, '').replace(/\bDemo:\s*/gi, '')
  : String(message.body ?? '');

export function preserveProfileMarkers(volunteer, edited) {
  if (!volunteer?.fictional) return edited;
  return {...edited, last_name: churchLabel(edited.last_name) + ' [Fictional]',
    availability: edited.availability === availabilityText(volunteer)
      ? volunteer.availability : edited.availability};
}
