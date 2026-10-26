"""IBGE population for electrified-per-capita maps."""
from src.connectors.reviewed_sources import seed_population
from src import db
from src.db import uf_population
from sqlalchemy import select

def sync_population():
    inserted = seed_population()
    with db.engine.connect() as c:
        rows = [dict(r) for r in c.execute(select(uf_population)).mappings()]
    return {'inserted': inserted, 'ufs': len(rows), 'year': rows[0]['year'] if rows else None}
