"""Retain only electrified rows; raw archives preserve the complete source."""
from alembic import op
from sqlalchemy import inspect,Column,JSON
revision='20260917_02'
down_revision='20260916_01'
branch_labels=None
depends_on=None
def upgrade():
    connection=op.get_bind();columns={x['name'] for x in inspect(connection).get_columns('pipeline_artifact')}
    for name in ('parse_rules','parse_stats'):
        if name not in columns:op.add_column('pipeline_artifact',Column(name,JSON))
    op.execute('CREATE TABLE IF NOT EXISTS pipeline_filtered_summary (artifact_id VARCHAR NOT NULL REFERENCES pipeline_artifact(id), block_index INTEGER NOT NULL, raw_name VARCHAR NOT NULL, status VARCHAR NOT NULL, quantity BIGINT NOT NULL, row_count BIGINT NOT NULL, electrified_hint INTEGER NOT NULL, PRIMARY KEY (artifact_id, block_index, raw_name))')
def downgrade():
    raise RuntimeError('Migração não destrutiva: preserve os arquivos e dados existentes.')
