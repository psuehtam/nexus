from alembic import context
from src import db,intelligence_schema,operational_schema,auth_schema,pipeline_schema

target_metadata=db.meta

if context.is_offline_mode():
    context.configure(url=db.engine.url.render_as_string(hide_password=False),target_metadata=target_metadata,literal_binds=True)
    with context.begin_transaction():context.run_migrations()
else:
    with db.engine.connect() as connection:
        context.configure(connection=connection,target_metadata=target_metadata)
        with context.begin_transaction():context.run_migrations()
