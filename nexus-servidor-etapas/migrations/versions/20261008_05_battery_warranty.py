"""Battery warranty and installation-scoped odometer observations."""
from alembic import op
import sqlalchemy as sa

revision = '20261008_05'
down_revision = '20260924_04'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('battery_warranty',
        sa.Column('battery_id', sa.String(), sa.ForeignKey('battery_asset.id'), primary_key=True),
        sa.Column('revision', sa.Integer(), nullable=False),
        sa.Column('data', sa.JSON(), nullable=False),
        sa.Column('updated_at', sa.String(), nullable=False))
    with op.batch_alter_table('battery_lifecycle_observation') as batch:
        batch.add_column(sa.Column('installation_id', sa.String(), nullable=True))
        batch.add_column(sa.Column('vehicle_id', sa.String(), nullable=True))
        batch.create_foreign_key('fk_lifecycle_installation', 'vehicle_battery_installation', ['installation_id'], ['id'])
        batch.create_foreign_key('fk_lifecycle_vehicle', 'vehicle_asset', ['vehicle_id'], ['id'])


def downgrade():
    with op.batch_alter_table('battery_lifecycle_observation') as batch:
        batch.drop_constraint('fk_lifecycle_vehicle', type_='foreignkey')
        batch.drop_constraint('fk_lifecycle_installation', type_='foreignkey')
        batch.drop_column('vehicle_id')
        batch.drop_column('installation_id')
    op.drop_table('battery_warranty')
