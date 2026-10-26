"""Add indexes used by the interactive geography filters."""
from alembic import op

revision = '20260924_04'
down_revision = '20260923_03'
branch_labels = None
depends_on = None


def upgrade():
    op.execute('CREATE INDEX IF NOT EXISTS idx_aggregate_snapshot_state_city ON vehicle_aggregate_observation(source_snapshot_id,state,municipality)')
    op.execute('CREATE INDEX IF NOT EXISTS idx_aggregate_snapshot_quantity ON vehicle_aggregate_observation(source_snapshot_id,quantity DESC,id)')
    op.execute('CREATE INDEX IF NOT EXISTS ix_heavy_stat_geography ON heavy_vehicle_statistic(reference_period,state,municipality)')


def downgrade():
    op.execute('DROP INDEX IF EXISTS ix_heavy_stat_geography')
    op.execute('DROP INDEX IF EXISTS idx_aggregate_snapshot_quantity')
    op.execute('DROP INDEX IF EXISTS idx_aggregate_snapshot_state_city')
