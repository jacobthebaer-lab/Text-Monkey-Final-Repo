"""Natural labels for newly prepared copy, never stored records or reviewed bodies."""
import re


def church_label(value):
    text = str(value or '')
    text = re.sub(r'^(?:(?:Synthetic|Demo|Test)\s*[:\-]?\s+)+', '', text, flags=re.I)
    text = re.sub(r'\s*\[(?:Fictional(?: history)?|Synthetic(?: \d{3})?)\]', '', text, flags=re.I)
    text = re.sub(r'\s*\((?:Synthetic|Demo|Test)\)\s*$', '', text, flags=re.I)
    return text.strip()
