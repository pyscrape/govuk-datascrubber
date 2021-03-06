import random
import time
import logging
import dns.resolver
import dns.name
import hashlib
from datetime import datetime

logger = logging.getLogger(__name__)


class ScrubWorkspaceInstance:
    def __init__(self, snapshot_finder, boto3_session, timeout=90, security_groups=None):
        timestamp = datetime.now()

        self.boto3_session = boto3_session
        self.rds_client = self.boto3_session.client('rds')
        self.snapshot_finder = snapshot_finder
        self.timeout = timeout
        self.password = "{0:x}".format(random.getrandbits(41 * 4))

        self.source_snapshot = self.snapshot_finder.get_snapshot()
        self.instance_identifier = "scrubber-{0}-{1}".format(
            self.source_snapshot['Engine'],
            hashlib.sha256(
                self.source_snapshot['DBInstanceIdentifier'].encode()
            ).hexdigest()[0:12]
        )
        self.final_snapshot_identifier = "scrubbed-{0}-{1}".format(
            self.source_snapshot['DBInstanceIdentifier'],
            timestamp.strftime("%Y-%m-%d-%H-%M")
        )
        self.source_instance = self.snapshot_finder.get_source_instance()

        self.instance = None
        self.deleted = False

        if type(security_groups) == str:
            self.security_groups = [security_groups]

        elif type(security_groups) == list:
            self.security_groups = security_groups

        else:
            self.security_groups = [
                sg['VpcSecurityGroupId'] for sg
                in self.source_instance['VpcSecurityGroups']
                if sg['Status'] == 'active'
            ]

        logger.info(
            "Initialised scrub workspace instance, DBInstanceIdentifier: %s",
            self.instance_identifier
        )

    def get_endpoint(self):
        i = self.get_instance()
        return i['Endpoint']

    def get_username(self):
        i = self.get_instance()
        return i['MasterUsername']

    def get_password(self):
        return self.password

    def get_instance(self):
        if self.instance is None:
            logger.info(
                "Instance %s doesn't exist yet, creating",
                self.instance_identifier
            )
            self.__create_instance()
            self.__apply_instance_modifications()

        return self.instance

    def cleanup(self, create_final_snapshot=True):
        if self.instance is not None and not self.deleted:
            rds = self.rds_client
            if create_final_snapshot:
                logger.info(
                    "Deleting RDS instance %s and creating final snapshot %s",
                    self.instance_identifier,
                    self.final_snapshot_identifier,
                )
                rds.delete_db_instance(
                    DBInstanceIdentifier=self.instance_identifier,
                    FinalDBSnapshotIdentifier=self.final_snapshot_identifier,
                )
                self.deleted = True
                self.__wait_for_final_snapshot()
            else:
                logger.info(
                    "Deleting RDS instance %s without final snapshot",
                    self.instance_identifier,
                )
                rds.delete_db_instance(
                    DBInstanceIdentifier=self.instance_identifier,
                    SkipFinalSnapshot=True,
                )
                self.deleted = True

    def delete_old_snapshots(self, number_to_keep):
        logger.info(
            "Looking for older snapshots of %s to clean up",
            self.instance_identifier,
        )

        response = self.rds_client.describe_db_snapshots(IncludeShared=True)
        snapshots = [
            s for s in response['DBSnapshots'] if s['DBInstanceIdentifier'] == self.instance_identifier
        ]
        snapshots.sort(
            key=lambda x: x.get('SnapshotCreateTime', 0),
            reverse=True
        )

        logger.info(
            "%d snapshots for %s exist in RDS",
            len(snapshots),
            self.instance_identifier,
        )

        snaps_to_delete = snapshots[number_to_keep:]

        logger.info(
            "%d older snapshots of %s identified for deletion (keeping %d)",
            len(snaps_to_delete),
            self.instance_identifier,
            number_to_keep
        )

        for snap in snaps_to_delete:
            logger.info("Deleting snapshot %s", snap['DBSnapshotIdentifier'])
            self.rds_client.delete_db_snapshot(
                DBSnapshotIdentifier=snap['DBSnapshotIdentifier'],
            )

    def __create_instance(self):
        rds = self.rds_client
        source_snapshot_id = self.snapshot_finder.get_snapshot_identifier()
        subnet_group_name = self.source_instance['DBSubnetGroup']['DBSubnetGroupName']

        logger.info(
            "Restoring instance %s from snapshot %s in subnet group %s. Timeout: %s minutes",
            self.instance_identifier,
            source_snapshot_id,
            subnet_group_name,
            self.timeout,
        )

        rds.restore_db_instance_from_db_snapshot(
            DBInstanceIdentifier=self.instance_identifier,
            DBSnapshotIdentifier=source_snapshot_id,
            DBSubnetGroupName=subnet_group_name,
            Tags=[
                {
                    'Key': 'scrubber',
                    'Value': 'scrubber'
                }
            ]
        )

        max_end_time = time.time() + 60 * self.timeout
        while time.time() <= max_end_time:
            poll_response = rds.describe_db_instances(
                DBInstanceIdentifier=self.instance_identifier
            )
            self.instance = poll_response['DBInstances'][0]

            logger.info(
                "Waiting for %s to become available, current status: '%s'",
                self.instance_identifier,
                self.instance['DBInstanceStatus'],
            )

            if self.instance['DBInstanceStatus'] == 'available':
                return
            else:
                time.sleep(10)

        raise TimeoutError(
            "Timed out creating RDS instance {0}".format(
                self.instance_identifier
            )
        )

    def __apply_instance_modifications(self):
        rds = self.rds_client

        logger.info(
            "Applying modifications to %s: %s",
            self.instance_identifier,
            {
                'VpcSecurityGroupIds': self.security_groups,
                'MasterUserPassword': '****',
                'BackupRetentionPeriod': 0,
            },
        )

        rds.modify_db_instance(
            DBInstanceIdentifier=self.instance_identifier,
            VpcSecurityGroupIds=self.security_groups,
            MasterUserPassword=self.password,
            BackupRetentionPeriod=0,
            ApplyImmediately=True,
        )

        max_end_time = time.time() + 60 * self.timeout
        while time.time() <= max_end_time:
            poll_response = rds.describe_db_instances(
                DBInstanceIdentifier=self.instance_identifier
            )
            pending = list(
                poll_response['DBInstances'][0]['PendingModifiedValues'].keys()
            )

            if len(pending) > 0:
                logger.info(
                    "Modifications to %s still pending: %s",
                    self.instance_identifier,
                    pending,
                )
                time.sleep(10)
            else:
                return

        raise TimeoutError(
            "Timed out applying changes to RDS instance {0}".format(
                self.instance_identifier
            )
        )

    def __wait_for_final_snapshot(self):
        rds = self.rds_client

        max_end_time = time.time() + 60 * self.timeout
        while time.time() <= max_end_time:
            try:
                logger.info(
                    "Waiting for snapshot %s to become available. Timeout: %s minutes",
                    self.final_snapshot_identifier,
                    self.timeout,
                )

                poll_response = rds.describe_db_snapshots(
                    DBSnapshotIdentifier=self.final_snapshot_identifier
                )

                if poll_response['DBSnapshots'][0]['Status'] == 'available':
                    logger.info(
                        "Snapshot %s is now available",
                        self.final_snapshot_identifier
                    )
                    return

                else:
                    time.sleep(10)
            except Exception as e:
                if e.response['Error']['Code'] == 'DBSnapshotNotFound':
                    time.sleep(10)
                else:
                    raise(e)


class RdsSnapshotFinder:
    rds_domain = dns.name.from_text('rds.amazonaws.com.')

    def __init__(self, boto3_session, hostname=None, source_instance_identifier=None, snapshot_identifier=None):
        self.boto3_session = boto3_session
        self.rds_client = self.boto3_session.client('rds')

        if (hostname is None and source_instance_identifier is None and snapshot_identifier is None):
            raise Exception("One of hostname, source_instance_identifier, snapshot_identifier must be provided")

        self.hostname = hostname
        self.source_instance_identifier = source_instance_identifier
        self.snapshot_identifier = snapshot_identifier

        self.rds_endpoint_address = None
        self.source_instance = None
        self.snapshot = None

        logger.info("Initialised RDS Snapshot Finder")

    def get_snapshot(self):
        if self.snapshot is None:
            snapshot_identifier = self.get_snapshot_identifier()

            response = self.rds_client.describe_db_snapshots(
                DBSnapshotIdentifier=snapshot_identifier,
            )

            if len(response['DBSnapshots']) == 0:
                raise Exception("Snapshot {0} not found".format(snapshot_identifier))

            self.snapshot = response['DBSnapshots'][0]
            self.source_instance_identifier = self.snapshot['DBInstanceIdentifier']

        return self.snapshot

    def get_snapshot_identifier(self):
        if self.snapshot_identifier is None:
            logger.info("Discovering snapshot identifier...")

            source_instance_id = self.get_source_instance_identifier()
            response = self.rds_client.describe_db_snapshots(
                DBInstanceIdentifier=source_instance_id,
            )

            if len(response['DBSnapshots']) == 0:
                raise Exception("No snapshots found")

            logger.debug(
                "Found %d snapshots for %s",
                len(response['DBSnapshots']),
                source_instance_id,
            )

            response['DBSnapshots'].sort(
                key=lambda x: x.get('SnapshotCreateTime', 0),
                reverse=True
            )
            most_recent = response['DBSnapshots'][0]
            self.snapshot_identifier = most_recent['DBSnapshotIdentifier']

            logger.info("Using snapshot %s", self.snapshot_identifier)

        return self.snapshot_identifier

    def get_source_instance_identifier(self):
        if self.source_instance_identifier is None:
            logger.info("Discovering source RDS instance identifier...")

            i = self.get_source_instance()
            self.source_instance_identifier = i['DBInstanceIdentifier']

            logger.info("Using source RDS instance %s", self.source_instance_identifier)

        return self.source_instance_identifier

    def get_source_instance(self):
        if self.source_instance is None:
            if self.source_instance_identifier is None:
                logger.info("Discovering source RDS instance...")

                rds_instances = self.rds_client.describe_db_instances()

                logger.debug(
                    "Enumerated %d RDS instances",
                    len(rds_instances['DBInstances'])
                )

                for instance in rds_instances['DBInstances']:
                    if 'Endpoint' in instance and instance['Endpoint']['Address'] == self.get_rds_endpoint_address():
                        logger.info(
                            "RDS instance %s matches endpoint address %s:%s",
                            instance['DBInstanceIdentifier'],
                            instance['Endpoint']['Address'],
                            instance['Endpoint']['Port'],
                        )

                        self.source_instance = instance
                        self.source_instance_identifier = instance['DBInstanceIdentifier']
                        break

                if self.source_instance is None:
                    raise Exception("Couldn't find an RDS instance matching endpoint address %s" % self.get_rds_endpoint_address())
            else:
                logger.info("Looking up RDS instance %s ...", self.source_instance_identifier)
                # An exception will be raised if the instance doesn't exist
                rds_instances = self.rds_client.describe_db_instances(
                    DBInstanceIdentifier=self.source_instance_identifier
                )
                self.source_instance = rds_instances['DBInstances'][0]

        return self.source_instance

    def get_hostname(self):
        if self.hostname is None:
            raise Exception("No hostname provided")

        return self.hostname

    def get_rds_endpoint_address(self):
        if self.rds_endpoint_address is None:
            logger.info("Discovering RDS endpoint address via DNS...")

            resolver = dns.resolver.Resolver()
            resolution = resolver.query(self.get_hostname())
            cname = resolution.canonical_name

            if not cname.is_subdomain(self.rds_domain):
                raise Exception("{0} is not a subdomain of RDS domain ({1})".format(
                    cname.to_text().rstrip('.'),
                    self.rds_domain
                ))

            self.rds_endpoint_address = cname.to_text().rstrip('.')
            logger.info(
                "Resolved RDS endpoint address of %s to %s",
                self.get_hostname(),
                self.rds_endpoint_address,
            )

        return self.rds_endpoint_address
