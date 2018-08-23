import logging
import psycopg2
import re

import datascrubber.tasks

logger = logging.getLogger(__name__)


class Postgresql:
    def __init__(self, workspace, db_suffix='_production'):
        self.scrub_functions = {
            'email-alert-api': datascrubber.tasks.scrub_email_alert_api,
            'publishing_api': datascrubber.tasks.scrub_publishing_api,
        }
        self.db_realnames = {}

        self.workspace = workspace
        self.db_suffix = db_suffix
        self.viable_tasks = None

        self._discover_available_dbs()

    def _get_connection(self, dbname):
        instance = self.workspace.get_instance()

        logger.info("Connecting to Postgres: %s", {
            "endpoint": "{0}:{1}".format(
                instance['Endpoint']['Address'],
                instance['Endpoint']['Port']
            ),
            "user": instance['MasterUsername'],
            "database": dbname,
        })

        connection = psycopg2.connect(
            user=instance['MasterUsername'],
            password=self.workspace.password,
            host=instance['Endpoint']['Address'],
            port=instance['Endpoint']['Port'],
            dbname=dbname,
        )
        return connection

    def _discover_available_dbs(self):
        logger.info("Looking for available databases in Postgres")

        cnx = self._get_connection('postgres')
        cursor = cnx.cursor()
        cursor.execute(
            "SELECT datname FROM pg_database "
            "WHERE datname NOT IN ("
            "  'template0', "
            "  'rdsadmin', "
            "  'postgres', "
            "  'template1' "
            ") AND datistemplate IS FALSE"
        )
        rows = cursor.fetchall()
        available_dbs = [r[0] for r in rows]
        logger.info("Databases found: %s", available_dbs)

        r = re.compile('{0}$'.format(self.db_suffix))
        for database_name in available_dbs:
            normalised_name = r.sub('', database_name)
            self.db_realnames[normalised_name] = database_name

    def get_viable_tasks(self):
        if self.viable_tasks is None:
            self.viable_tasks = list(
                set(self.scrub_functions.keys()) &
                set(self.db_realnames.keys())
            )
            logger.info("Viable scrub tasks: %s", self.viable_tasks)

        return self.viable_tasks

    def run_task(self, task):
        if task not in self.get_viable_tasks():
            logger.error(
                "%s is not a viable scrub task for %s",
                task, self.__class__
            )
            return False

        logger.info("Running scrub task: %s", task)
        cnx = self._get_connection(self.db_realnames[task])
        cursor = cnx.cursor()
        try:
            self.scrub_functions[task](cursor)
            cnx.commit()
            return True
        except Exception as e:
            logger.error("Error running scrub task %s: %s", task, e)
            cnx.rollback()
            return False
