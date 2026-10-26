"""Fenabrave monthly registrations: sales, not SENATRAN stock."""
from src.connectors.reviewed_sources import seed_fenabrave, read_json
from src import db
from src.db import registrations
from sqlalchemy import select, insert, delete

def ingest_reviewed():
    return {'inserted': seed_fenabrave()}

def ingest_rows(reference, provider, items, notes=None):
    rows = [dict(reference=reference, provider=provider, brand=item.get('brand'), model=item.get('model'),
                 propulsion=item['propulsion'], quantity=int(item['quantity']), notes=notes) for item in items]
    with db.engine.begin() as c:
        c.execute(delete(registrations).where(registrations.c.reference == reference, registrations.c.provider == provider))
        c.execute(insert(registrations), rows)
    return {'reference': reference, 'rows': len(rows), 'vehicles': sum(r['quantity'] for r in rows)}
