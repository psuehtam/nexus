import re
import unicodedata
from difflib import SequenceMatcher

def normalize(value):
    value = unicodedata.normalize('NFKD', str(value or '')).encode('ascii', 'ignore').decode().upper().strip()
    value = re.sub(r'^(?:I|IMP)\s*/\s*', '', value)
    return re.sub(r'[^A-Z0-9]+', ' ', value).strip()

def _propulsion(model):
    return model.get('propulsion') or model.get('category')

def _generic_version(value):
    return normalize(value) in ('', 'NAO INFORMADA', 'NAO INFORMADO')

def match(raw_name, year, catalog):
    """Never silently assign a battery to a fuzzy or wrong-year candidate."""
    name = normalize(raw_name)
    exact = [m for m in catalog if name in [normalize(a) for a in (m.get('aliases') or [])]
             and year is not None and m['year_start'] <= year <= m['year_end']]
    if len(exact) == 1:
        return {'model': exact[0], 'confidence': 'A', 'method': 'alias_validado', 'score': 1.0}
    from src.catalog import classify
    found = classify(raw_name)
    if found and year is not None:
        brand, model_name, version, propulsion = found
        leftover = normalize(version)
        family = [m for m in catalog if m['brand'] == brand and m['model'] == model_name
                  and _propulsion(m) == propulsion
                  and m['year_start'] is not None and m['year_end'] is not None
                  and m['year_start'] <= year <= m['year_end']]
        def version_ok(item):
            rule_version = normalize(item.get('version'))
            if _generic_version(rule_version):
                return True
            return leftover == rule_version or rule_version in name
        resolved = [m for m in family if version_ok(m)]
        specific = [m for m in resolved if not _generic_version(m.get('version'))
                    and (leftover == normalize(m.get('version')) or normalize(m.get('version')) in name)]
        chosen = specific if len(specific) == 1 else resolved
        if len(specific) != 1 and len(resolved) != 1:
            chosen = []
        if len(chosen) == 1:
            return {'model': chosen[0], 'confidence': 'A', 'method': 'familia_ano', 'score': 1.0}
    candidates = [(max((SequenceMatcher(None, name, normalize(a)).ratio() for a in (m.get('aliases') or [])), default=0), m) for m in catalog]
    score, candidate = max(candidates, key=lambda x: x[0], default=(0, None))
    return {'model': None, 'suggestion': candidate, 'confidence': 'C' if score >= .75 else 'D',
            'method': 'revisao_necessaria' if score >= .75 else 'sem_match', 'score': round(score, 3)}
