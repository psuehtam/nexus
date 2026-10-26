"""Rank OpenEV candidates against reviewed electrified families; never auto-approve."""
from src import db
from src.db import technical_candidates
from src.catalog import classify, FAMILIES
from sqlalchemy import select

def shortlist(limit=50):
    families = {(brand.lower(), model.lower()) for brand, model, _, _ in FAMILIES}
    with db.engine.connect() as c:
        rows = [dict(r) for r in c.execute(select(technical_candidates)).mappings()]
    ranked = []
    for row in rows:
        brand = (row.get('brand') or '').lower()
        model = (row.get('model') or '').lower()
        raw_name = f"{row.get('brand') or ''} {row.get('model') or ''}"
        found = classify(raw_name)
        overlap = any(brand.startswith(fb) or fb.startswith(brand) for fb, _ in families) and any(fm in model or model in fm for _, fm in families)
        if found or overlap:
            battery = (row.get('raw') or {}).get('battery') or {}
            ranked.append({
                'id': row['id'], 'brand': row['brand'], 'model': row['model'], 'year': row['year'],
                'status': row['status'], 'matched_family': found[0:2] if found else None,
                'capacity_kwh': battery.get('pack_capacity_kwh_gross') or battery.get('pack_capacity_kwh_net'),
                'chemistry': battery.get('chemistry'), 'part_number': battery.get('model_name'),
            })
    ranked.sort(key=lambda item: ((item['matched_family'] is None), item['brand'] or '', item['model'] or ''))
    return ranked[:limit]
