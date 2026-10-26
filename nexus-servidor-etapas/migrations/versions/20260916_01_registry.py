"""Add permanent registry, passport revisions, sessions and durable pipeline.

Existing legacy tables are retained. This additive baseline is intentionally
non-destructive; subsequent schema changes require explicit new revisions.
"""
from alembic import op
revision='20260916_01'
down_revision=None
branch_labels=None
depends_on=None

def upgrade():
    # Baseline table definitions are frozen in the adjacent DDL manifest.
    import json
    from pathlib import Path
    from sqlalchemy import text
    ddl=json.loads((Path(__file__).parent/'20260916_01_ddl.json').read_text(encoding='utf-8'))
    connection=op.get_bind()
    for sql in ddl[connection.dialect.name]:connection.execute(text(sql))

def downgrade():
    raise RuntimeError('Preserve dados novos. Use recuperação documentada; esta migração não apaga registros.')
