from django.db import migrations


OBSERVATION_TABLES = (
    "insights_publicationperformanceobservation",
    "insights_channelperformanceobservation",
    "insights_searchvisibilityobservation",
    "insights_searchindexobservation",
    "insights_commerceobservation",
    "insights_geometricobservation",
)


def create_triggers(apps, schema_editor):
    vendor = schema_editor.connection.vendor
    if vendor == "sqlite":
        schema_editor.execute(
            """
            CREATE TRIGGER insights_run_metric_domain_insert
            BEFORE INSERT ON insights_metriccollectionrunmetric
            WHEN NOT EXISTS (
                SELECT 1
                FROM insights_metriccollectionrun run
                JOIN insights_metricdefinition metric
                  ON metric.id = NEW.metric_definition_id
                WHERE run.id = NEW.collection_run_id
                  AND run.data_domain = NEW.data_domain
                  AND metric.data_domain = NEW.data_domain
            )
            BEGIN
                SELECT RAISE(ABORT, 'run, metric, and link must use one exact data domain');
            END;
            """
        )
        for operation in ("UPDATE", "DELETE"):
            schema_editor.execute(
                f"""
                CREATE TRIGGER insights_run_metric_immutable_{operation.lower()}
                BEFORE {operation} ON insights_metriccollectionrunmetric
                BEGIN
                    SELECT RAISE(ABORT, 'run-metric links are append-only');
                END;
                """
            )
        for table in OBSERVATION_TABLES:
            suffix = table.removeprefix("insights_")
            schema_editor.execute(
                f"""
                CREATE TRIGGER insights_{suffix}_domain_insert
                BEFORE INSERT ON {table}
                WHEN NOT EXISTS (
                    SELECT 1
                    FROM insights_metricdefinition metric
                    JOIN insights_metriccollectionrun run
                      ON run.id = NEW.collection_run_id
                    JOIN insights_metriccollectionrunmetric link
                      ON link.collection_run_id = run.id
                     AND link.metric_definition_id = metric.id
                     AND link.data_domain = NEW.data_domain
                    WHERE metric.id = NEW.metric_definition_id
                      AND metric.data_domain = NEW.data_domain
                      AND run.data_domain = NEW.data_domain
                )
                BEGIN
                    SELECT RAISE(ABORT, 'observation metric/run domain mismatch');
                END;
                """
            )
            for operation in ("UPDATE", "DELETE"):
                schema_editor.execute(
                    f"""
                    CREATE TRIGGER insights_{suffix}_immutable_{operation.lower()}
                    BEFORE {operation} ON {table}
                    BEGIN
                        SELECT RAISE(ABORT, 'observation facts are append-only');
                    END;
                    """
                )
        return

    if vendor == "postgresql":
        schema_editor.execute(
            """
            CREATE FUNCTION insights_enforce_run_metric_domain() RETURNS trigger AS $$
            BEGIN
                IF NOT EXISTS (
                    SELECT 1
                    FROM insights_metriccollectionrun run
                    JOIN insights_metricdefinition metric
                      ON metric.id = NEW.metric_definition_id
                    WHERE run.id = NEW.collection_run_id
                      AND run.data_domain = NEW.data_domain
                      AND metric.data_domain = NEW.data_domain
                ) THEN
                    RAISE EXCEPTION 'run, metric, and link must use one exact data domain'
                        USING ERRCODE = '23514';
                END IF;
                RETURN NEW;
            END;
            $$ LANGUAGE plpgsql;
            """
        )
        schema_editor.execute(
            """
            CREATE TRIGGER insights_run_metric_domain_guard
            BEFORE INSERT OR UPDATE ON insights_metriccollectionrunmetric
            FOR EACH ROW EXECUTE FUNCTION insights_enforce_run_metric_domain();
            """
        )
        schema_editor.execute(
            """
            CREATE FUNCTION insights_enforce_observation_domain() RETURNS trigger AS $$
            BEGIN
                IF NOT EXISTS (
                    SELECT 1
                    FROM insights_metricdefinition metric
                    JOIN insights_metriccollectionrun run
                      ON run.id = NEW.collection_run_id
                    JOIN insights_metriccollectionrunmetric link
                      ON link.collection_run_id = run.id
                     AND link.metric_definition_id = metric.id
                     AND link.data_domain = NEW.data_domain
                    WHERE metric.id = NEW.metric_definition_id
                      AND metric.data_domain = NEW.data_domain
                      AND run.data_domain = NEW.data_domain
                ) THEN
                    RAISE EXCEPTION 'observation metric/run domain mismatch'
                        USING ERRCODE = '23514';
                END IF;
                RETURN NEW;
            END;
            $$ LANGUAGE plpgsql;
            """
        )
        schema_editor.execute(
            """
            CREATE FUNCTION insights_reject_observation_mutation() RETURNS trigger AS $$
            BEGIN
                RAISE EXCEPTION 'observation facts are append-only'
                    USING ERRCODE = '23514';
            END;
            $$ LANGUAGE plpgsql;
            """
        )
        schema_editor.execute(
            """
            CREATE TRIGGER insights_run_metric_immutable_guard
            BEFORE UPDATE OR DELETE ON insights_metriccollectionrunmetric
            FOR EACH ROW EXECUTE FUNCTION insights_reject_observation_mutation();
            """
        )
        for table in OBSERVATION_TABLES:
            suffix = table.removeprefix("insights_")
            schema_editor.execute(
                f"""
                CREATE TRIGGER insights_{suffix}_domain_guard
                BEFORE INSERT ON {table}
                FOR EACH ROW EXECUTE FUNCTION insights_enforce_observation_domain();
                """
            )
            schema_editor.execute(
                f"""
                CREATE TRIGGER insights_{suffix}_immutable_guard
                BEFORE UPDATE OR DELETE ON {table}
                FOR EACH ROW EXECUTE FUNCTION insights_reject_observation_mutation();
                """
            )


def drop_triggers(apps, schema_editor):
    vendor = schema_editor.connection.vendor
    if vendor == "sqlite":
        schema_editor.execute("DROP TRIGGER IF EXISTS insights_run_metric_domain_insert;")
        schema_editor.execute("DROP TRIGGER IF EXISTS insights_run_metric_immutable_update;")
        schema_editor.execute("DROP TRIGGER IF EXISTS insights_run_metric_immutable_delete;")
        for table in OBSERVATION_TABLES:
            suffix = table.removeprefix("insights_")
            for name in (
                f"insights_{suffix}_domain_insert",
                f"insights_{suffix}_immutable_update",
                f"insights_{suffix}_immutable_delete",
            ):
                schema_editor.execute(f"DROP TRIGGER IF EXISTS {name};")
        return
    if vendor == "postgresql":
        schema_editor.execute(
            "DROP TRIGGER IF EXISTS insights_run_metric_domain_guard ON insights_metriccollectionrunmetric;"
        )
        schema_editor.execute(
            "DROP TRIGGER IF EXISTS insights_run_metric_immutable_guard ON insights_metriccollectionrunmetric;"
        )
        schema_editor.execute("DROP FUNCTION IF EXISTS insights_enforce_run_metric_domain();")
        for table in OBSERVATION_TABLES:
            suffix = table.removeprefix("insights_")
            schema_editor.execute(f"DROP TRIGGER IF EXISTS insights_{suffix}_domain_guard ON {table};")
            schema_editor.execute(f"DROP TRIGGER IF EXISTS insights_{suffix}_immutable_guard ON {table};")
        schema_editor.execute("DROP FUNCTION IF EXISTS insights_enforce_observation_domain();")
        schema_editor.execute("DROP FUNCTION IF EXISTS insights_reject_observation_mutation();")


class Migration(migrations.Migration):
    dependencies = [("insights", "0001_initial")]

    operations = [migrations.RunPython(create_triggers, drop_triggers)]
