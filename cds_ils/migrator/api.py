# -*- coding: utf-8 -*-
#
# This file is part of Invenio.
# Copyright (C) 2019-2020 CERN.
#
# CDS-ILS is free software; you can redistribute it and/or modify it
# under the terms of the MIT License; see LICENSE file for more details.

"""CDS-ILS migrator API."""

import json
import logging
import uuid
from contextlib import contextmanager

import click
from elasticsearch import VERSION as ES_VERSION
from elasticsearch_dsl import Q
from flask import current_app
from invenio_app_ils.circulation.api import IlsCirculationLoanIdProvider
from invenio_app_ils.documents.api import Document, DocumentIdProvider
from invenio_app_ils.documents.search import DocumentSearch
from invenio_app_ils.errors import IlsValidationError
from invenio_app_ils.ill.api import Library, LibraryIdProvider
from invenio_app_ils.internal_locations.api import InternalLocation, \
    InternalLocationIdProvider
from invenio_app_ils.internal_locations.search import InternalLocationSearch
from invenio_app_ils.items.api import ITEM_PID_TYPE, Item, ItemIdProvider
from invenio_app_ils.items.search import ItemSearch
from invenio_app_ils.patrons.api import SystemAgent
from invenio_app_ils.patrons.indexer import PatronIndexer
from invenio_app_ils.patrons.search import PatronsSearch
from invenio_app_ils.proxies import current_app_ils
from invenio_app_ils.records_relations.api import RecordRelationsParentChild
from invenio_app_ils.relations.api import MULTIPART_MONOGRAPH_RELATION, \
    SERIAL_RELATION
from invenio_app_ils.series.api import Series, SeriesIdProvider
from invenio_app_ils.series.search import SeriesSearch
from invenio_base.app import create_cli
from invenio_circulation.api import Loan
from invenio_db import db
from invenio_indexer.api import RecordIndexer
from invenio_migrator.cli import _loadrecord
from invenio_oauthclient.models import RemoteAccount

from cds_ils.migrator.errors import DocumentMigrationError, \
    ItemMigrationError, LoanMigrationError, MultipartMigrationError, \
    UserMigrationError
from cds_ils.migrator.records import CDSRecordDumpLoader
from cds_ils.migrator.utils import clean_item_record
from cds_ils.patrons.api import Patron

migrated_logger = logging.getLogger("migrated_documents")


@contextmanager
def commit():
    """Commit transaction or rollback in case of an exception."""
    try:
        yield
        db.session.commit()
    except Exception:
        print("Rolling back changes...")
        db.session.rollback()
        raise


def reindex_pidtype(pid_type):
    """Reindex records with the specified pid_type."""
    click.echo('Indexing pid type "{}"...'.format(pid_type))
    cli = create_cli()
    runner = current_app.test_cli_runner()
    runner.invoke(
        cli,
        "index reindex --pid-type {} --yes-i-know".format(pid_type),
        catch_exceptions=False,
    )
    runner.invoke(cli, "index run", catch_exceptions=False)
    click.echo("Indexing completed!")


def bulk_index_records(records):
    """Bulk index a list of records."""
    indexer = RecordIndexer()

    click.echo("Bulk indexing {} records...".format(len(records)))
    indexer.bulk_index([str(r.id) for r in records])
    indexer.process_bulk_queue()
    click.echo("Indexing completed!")


def model_provider_by_rectype(rectype):
    """Return the correct model and PID provider based on the rectype."""
    if rectype in ("serial", "multipart"):
        return Series, SeriesIdProvider
    elif rectype == "document":
        return Document, DocumentIdProvider
    elif rectype == "internal_location":
        return InternalLocation, InternalLocationIdProvider
    elif rectype == "library":
        return Library, LibraryIdProvider
    elif rectype == "item":
        return Item, ItemIdProvider
    elif rectype == "loan":
        return Loan, IlsCirculationLoanIdProvider
    else:
        raise ValueError("Unknown rectype: {}".format(rectype))


def import_parents_from_file(dump_file, rectype, include):
    """Load parent records from file."""
    model, provider = model_provider_by_rectype(rectype)
    include_keys = None if include is None else include.split(",")
    with click.progressbar(json.load(dump_file).items()) as bar:
        records = []
        for key, parent in bar:
            if "legacy_recid" in parent:
                click.echo(
                    'Importing parent "{0}({1})"...'.format(
                        parent["legacy_recid"], rectype
                    )
                )
            else:
                click.echo(
                    'Importing parent "{0}({1})"...'.format(
                        parent["title"], rectype
                    )
                )
            if include_keys is None or key in include_keys:
                has_children = parent.get("_migration", {}).get("children", [])
                has_volumes = parent.get("_migration", {}).get("volumes", [])
                if rectype == "serial" and has_children:
                    record = import_record(parent, model, provider)
                    records.append(record)
                elif rectype == "multipart" and has_volumes:
                    record = import_record(parent, model, provider)
                    records.append(record)
    # Index all new parent records
    bulk_index_records(records)


def import_record(dump, model, pid_provider, legacy_id_key="legacy_recid"):
    """Import record in database."""
    record = CDSRecordDumpLoader.create(
        dump, model, pid_provider, legacy_id_key
    )
    return record


def import_documents_from_record_file(sources, include):
    """Import documents from records file generated by CDS-Migrator-Kit."""
    include = include if include is None else include.split(",")
    records = []
    for idx, source in enumerate(sources, 1):
        click.echo(
            "({}/{}) Migrating documents in {}...".format(
                idx, len(sources), source.name
            )
        )
        model, provider = model_provider_by_rectype("document")
        include_keys = None if include is None else include.split(",")
        with click.progressbar(json.load(source).items()) as bar:
            records = []
            for key, parent in bar:
                click.echo(
                    'Importing document "{}"...'.format(parent["legacy_recid"])
                )
                if include_keys is None or key in include_keys:
                    record = import_record(parent, model, provider)
                    records.append(record)
    # Index all new parent records
    bulk_index_records(records)


def import_documents_from_dump(sources, source_type, eager, include):
    """Load records."""
    include = include if include is None else include.split(",")
    for idx, source in enumerate(sources, 1):
        click.echo(
            "({}/{}) Migrating documents in {}...".format(
                idx, len(sources), source.name
            )
        )
        data = json.load(source)
        with click.progressbar(data) as records:
            for item in records:
                click.echo('Processing document "{}"...'.format(item["recid"]))
                if include is None or str(item["recid"]) in include:
                    try:
                        _loadrecord(item, source_type, eager=eager)
                        migrated_logger.warning(
                            "#RECID {0}: OK".format(item["recid"])
                        )
                    except IlsValidationError as e:
                        document_logger = logging.getLogger("documents")
                        document_logger.error(
                            "@RECID: {0} FATAL: {1}".format(
                                item["recid"],
                                str(e.original_exception.message),
                            )
                        )
                    except Exception as e:
                        document_logger = logging.getLogger("documents")
                        document_logger.error(
                            "@RECID: {0} ERROR: {1}".format(
                                item["recid"], str(e)
                            )
                        )


def import_internal_locations_from_json(
    dump_file, include, rectype="internal_location"
):
    """Load parent records from file."""
    dump_file = dump_file[0]
    model, provider = model_provider_by_rectype(rectype)
    library_model, library_provider = model_provider_by_rectype("library")

    include_ids = None if include is None else include.split(",")
    with click.progressbar(json.load(dump_file)) as bar:
        records = []
        for record in bar:
            click.echo(
                'Importing internal location "{0}({1})"...'.format(
                    record["legacy_id"], rectype
                )
            )
            if include_ids is None or record["legacy_id"] in include_ids:
                # remove the library type as it is not a part of the data model
                library_type = record.pop("type", None)
                record["legacy_id"] = str(record["legacy_id"])
                if library_type == "external":
                    # if the type is external => ILL Library
                    record = import_record(
                        record,
                        library_model,
                        library_provider,
                        legacy_id_key="legacy_id",
                    )
                    records.append(record)
                else:
                    (
                        location_pid_value,
                        _,
                    ) = current_app_ils.get_default_location_pid
                    record["location_pid"] = location_pid_value
                    record = import_record(
                        record, model, provider, legacy_id_key="legacy_id"
                    )
                    records.append(record)
    # Index all new internal location and libraries records
    bulk_index_records(records)


def import_items_from_json(dump_file, include, rectype="item"):
    """Load items from json file."""
    dump_file = dump_file[0]
    model, provider = model_provider_by_rectype(rectype)

    include_ids = None if include is None else include.split(",")
    with click.progressbar(json.load(dump_file)) as bar:
        for record in bar:
            click.echo(
                'Importing item "{0}({1})"...'.format(
                    record["barcode"], rectype
                )
            )
            if include_ids is None or record["barcode"] in include_ids:

                int_loc_pid_value = get_internal_location_by_legacy_recid(
                    record["id_crcLIBRARY"]
                ).pid.pid_value

                record["internal_location_pid"] = int_loc_pid_value
                try:
                    record["document_pid"] = get_document_by_legacy_recid(
                        record["id_bibrec"]
                    ).pid.pid_value
                except DocumentMigrationError:
                    continue
                try:
                    clean_item_record(record)
                except ItemMigrationError as e:
                    click.secho(str(e), fg="red")
                    continue
                try:
                    # check if the item already there
                    item = get_item_by_barcode(record["barcode"])
                    if item:
                        click.secho(
                            "Item {0}) already exists with pid: {1}".format(
                                record["barcode"], item.pid
                            ),
                            fg="blue",
                        )
                        continue
                except ItemMigrationError:
                    record = import_record(
                        record, model, provider, legacy_id_key="barcode"
                    )
                try:
                    # without this script is very slow
                    db.session.commit()
                except Exception:
                    db.session.rollback()


def import_users_from_json(dump_file):
    """Imports additional user data from JSON."""
    dump_file = dump_file[0]
    with click.progressbar(json.load(dump_file)) as bar:
        for record in bar:
            click.echo(
                'Importing user "{0}({1})"...'.format(
                    record["id"], record["email"]
                )
            )
            user = get_user_by_person_id(record["ccid"])
            if not user:
                click.secho(
                    "User {0}({1}) not synced via LDAP".format(
                        record["id"], record["email"]
                    ),
                    fg="red",
                )
                continue
                # todo uncomment when more data
                # raise UserMigrationError
            else:
                client_id = current_app.config["CERN_APP_OPENID_CREDENTIALS"][
                    "consumer_key"
                ]
                account = RemoteAccount.get(
                    user_id=user.id, client_id=client_id
                )
                extra_data = account.extra_data
                # add legacy_id information
                account.extra_data.update(legacy_id=record["id"], **extra_data)
                db.session.add(account)
                patron = Patron(user.id)
                PatronIndexer().index(patron)
        db.session.commit()


def import_loans_from_json(dump_file):
    """Imports loan objects from JSON."""
    dump_file = dump_file[0]
    loans = []

    (
        default_location_pid_value,
        _,
    ) = current_app_ils.get_default_location_pid

    with click.progressbar(json.load(dump_file)) as bar:
        for record in bar:
            click.echo('Importing loan "{0}"...'.format(record["legacy_id"]))
            user = get_user_by_legacy_id(record["id_crcBORROWER"])
            if not user:
                patron_pid = SystemAgent.id
            else:
                patron_pid = user.pid
            try:
                item = get_item_by_barcode(record["item_barcode"])
            except ItemMigrationError:
                continue
                # Todo uncomment when more data
                # raise LoanMigrationError(
                #    'no item found with the barcode {} for loan {}'.format(
                #        record['item_barcode'], record['legacy_id']))

            # additional check if the loan refers to the same document
            # as it is already attached to the item
            document_pid = item.get("document_pid")
            document = Document.get_record_by_pid(document_pid)
            if record["legacy_document_id"] is None:
                raise LoanMigrationError(
                    "no document id for loan {}".format(record["legacy_id"])
                )
            if (
                document.get("legacy_recid", None)
                != record["legacy_document_id"]
            ):
                # this might happen when record merged or migrated,
                # the already migrated document should take precedence
                click.secho(
                    "inconsistent document dependencies for loan {}".format(
                        record["legacy_id"]
                    ),
                    fg="blue",
                )

            # create a loan

            if record["status"] == "on loan":
                loan_dict = dict(
                    patron_pid=str(patron_pid),
                    transaction_location_pid=default_location_pid_value,
                    transaction_user_pid=str(SystemAgent.id),
                    document_pid=document_pid,
                    item_pid=item.pid,
                    start_date=record["start_date"],
                    end_date=record["end_date"],
                    state="ITEM_ON_LOAN",
                    transaction_date=record["start_date"],
                )
            elif record["status"] == "returned":
                loan_dict = dict(
                    patron_pid=str(patron_pid),
                    transaction_location_pid=default_location_pid_value,
                    transaction_user_pid=str(SystemAgent.id),
                    transaction_date=record["returned_on"],
                    document_pid=document_pid,
                    item_pid=item.pid,
                    start_date=record["start_date"],
                    end_date=record["returned_on"],
                    state="ITEM_RETURNED",
                )
            else:
                raise LoanMigrationError(
                    "Unkown loan state for record {0}: {1}".format(
                        record["legacy_id"], record["state"]
                    )
                )
            model, provider = model_provider_by_rectype("loan")
            try:
                loan = import_record(loan_dict, model, provider)
            except Exception as e:
                raise e
            try:
                # without this script is very slow
                db.session.commit()
            except Exception:
                db.session.rollback()
    return loans


def get_item_by_barcode(barcode):
    """Retrieve item object by barcode."""
    search = ItemSearch().query(
        "bool",
        filter=[
            Q("term", barcode=barcode),
        ],
    )
    result = search.execute()
    hits_total = result.hits.total
    if not result.hits or hits_total < 1:
        click.secho("no item found with barcode {}".format(barcode), fg="red")
        raise ItemMigrationError(
            "no item found with barcode {}".format(barcode)
        )
    elif hits_total > 1:
        raise ItemMigrationError(
            "found more than one item with barcode {}".format(barcode)
        )
    else:
        return Item.get_record_by_pid(result.hits[0].pid)


def get_user_by_person_id(person_id):
    """Get ES object of the patron."""
    search = PatronsSearch().query(
        "bool",
        filter=[
            Q("term", person_id=person_id),
        ],
    )
    results = search.execute()
    hits_total = results.hits.total
    if not results.hits or hits_total < 1:
        click.secho(
            "no user found with person_id {}".format(person_id), fg="red"
        )
        return None
    elif hits_total > 1:
        raise UserMigrationError(
            "found more than one user with person_id {}".format(person_id)
        )
    else:
        return results.hits[0]


def get_user_by_legacy_id(legacy_id):
    """Get ES object of the patron."""
    search = PatronsSearch().query(
        "bool",
        filter=[
            Q("term", legacy_id=legacy_id),
        ],
    )
    results = search.execute()
    hits_total = results.hits.total
    if not results.hits or hits_total < 1:
        click.secho(
            "no user found with legacy_id {}".format(legacy_id), fg="red"
        )
        return None
    elif hits_total > 1:
        raise UserMigrationError(
            "found more than one user with legacy_id {}".format(legacy_id)
        )
    else:
        return results.hits[0]


def get_internal_location_by_legacy_recid(legacy_recid):
    """Search for internal location by legacy id."""
    search = InternalLocationSearch().query(
        "bool", filter=[Q("term", legacy_id=legacy_recid)]
    )
    result = search.execute()
    hits_total = result.hits.total
    if not result.hits or hits_total < 1:
        click.secho(
            "no internal location found with legacy id {}".format(
                legacy_recid
            ),
            fg="red",
        )
        raise ItemMigrationError(
            "no internal location found with legacy id {}".format(legacy_recid)
        )
    elif hits_total > 1:
        raise ItemMigrationError(
            "found more than one internal location with legacy id {}".format(
                legacy_recid
            )
        )
    else:
        return InternalLocation.get_record_by_pid(result.hits[0].pid)


def get_multipart_by_legacy_recid(recid):
    """Search multiparts by its legacy recid."""
    search = SeriesSearch().query(
        "bool",
        filter=[
            Q("term", mode_of_issuance="MULTIPART_MONOGRAPH"),
            Q("term", legacy_recid=recid),
        ],
    )
    result = search.execute()
    hits_total = result.hits.total
    if not result.hits or hits_total < 1:
        click.secho(
            "no multipart found with legacy recid {}".format(recid), fg="red"
        )
        # TODO uncomment with cleaner data
        # raise MultipartMigrationError(
        #     'no multipart found with legacy recid {}'.format(recid))
    elif hits_total > 1:
        raise MultipartMigrationError(
            "found more than one multipart with recid {}".format(recid)
        )
    else:
        return Series.get_record_by_pid(result.hits[0].pid)


def get_document_by_legacy_recid(legacy_recid):
    """Search documents by its legacy recid."""
    search = DocumentSearch().query(
        "bool", filter=[Q("term", legacy_recid=legacy_recid)]
    )
    result = search.execute()
    hits_total = result.hits.total
    if not result.hits or hits_total < 1:
        click.secho(
            "no document found with legacy recid {}".format(legacy_recid),
            fg="red",
        )
        raise DocumentMigrationError(
            "no document found with legacy recid {}".format(legacy_recid)
        )
    elif hits_total > 1:
        click.secho(
            "no document found with legacy recid {}".format(legacy_recid),
            fg="red",
        )
        raise DocumentMigrationError(
            "found more than one document with recid {}".format(legacy_recid)
        )
    else:
        click.secho(
            "! document found with legacy recid {}".format(legacy_recid),
            fg="green",
        )
        return Document.get_record_by_pid(result.hits[0].pid)


def create_multipart_volumes(pid, multipart_legacy_recid, migration_volumes):
    """Create multipart volume documents."""
    volumes = {}
    # Combine all volume data by volume number
    click.echo("Creating volume for {}...".format(multipart_legacy_recid))
    for obj in migration_volumes:
        volume_number = obj["volume"]
        if volume_number not in volumes:
            volumes[volume_number] = {}
        volume = volumes[volume_number]
        for key in obj:
            if key != "volume":
                if key in volume:
                    raise KeyError(
                        'Duplicate key "{}" for multipart {}'.format(
                            key, multipart_legacy_recid
                        )
                    )
                volume[key] = obj[key]

    volume_numbers = iter(sorted(volumes.keys()))

    # Re-use the current record for the first volume
    # TODO review this - there are more cases of multiparts
    first_volume = next(volume_numbers)
    first = Document.get_record_by_pid(pid)
    if "title" in volumes[first_volume]:
        first["title"] = volumes[first_volume]["title"]
        first["volume"] = first_volume
    first["_migration"]["multipart_legacy_recid"] = multipart_legacy_recid
    # to be tested
    if "legacy_recid" in first:
        del first["legacy_recid"]
    first.commit()
    yield first

    # Create new records for the rest
    for number in volume_numbers:
        temp = first.copy()
        temp["title"] = volumes[number]["title"]
        temp["volume"] = number
        record_uuid = uuid.uuid4()
        provider = DocumentIdProvider.create(
            object_type="rec", object_uuid=record_uuid
        )
        temp["pid"] = provider.pid.pid_value
        record = Document.create(temp, record_uuid)
        record.commit()
        yield record


def create_parent_child_relation(parent, child, relation_type, volume):
    """Create parent child relations."""
    rr = RecordRelationsParentChild()
    click.echo(
        "Creating relations: {0} - {1}".format(parent["pid"], child["pid"])
    )
    rr.add(
        parent=parent,
        child=child,
        relation_type=relation_type,
        volume=str(volume) if volume else None,
    )


def link_and_create_multipart_volumes():
    """Link and create multipart volume records."""
    click.echo("Creating document volumes and multipart relations...")
    search = DocumentSearch().filter("term", _migration__is_multipart=True)
    for hit in search.scan():
        if "legacy_recid" not in hit:
            continue
        click.secho(
            "Linking multipart {}...".format(hit.legacy_recid), fg="green"
        )
        multipart = get_multipart_by_legacy_recid(hit.legacy_recid)
        documents = create_multipart_volumes(
            hit.pid, hit.legacy_recid, hit._migration.volumes
        )

        for document in documents:
            if document and multipart:
                click.echo(
                    "Creating relations: {0} - {1}".format(
                        multipart["pid"], document["pid"]
                    )
                )
                create_parent_child_relation(
                    multipart,
                    document,
                    MULTIPART_MONOGRAPH_RELATION,
                    document["volume"],
                )


def get_serials_by_child_recid(recid):
    """Search serials by children recid."""
    search = SeriesSearch().query(
        "bool",
        filter=[
            Q("term", mode_of_issuance="SERIAL"),
            Q("term", _migration__children=recid),
        ],
    )
    for hit in search.scan():
        yield Series.get_record_by_pid(hit.pid)


def get_migrated_volume_by_serial_title(record, title):
    """Get volume number by serial title."""
    for serial in record["_migration"]["serials"]:
        if serial["title"] == title:
            return serial.get("volume", None)
    raise DocumentMigrationError(
        'Unable to find volume number in record {} by title "{}"'.format(
            record["pid"], title
        )
    )


def link_documents_and_serials():
    """Link documents/multiparts and serials."""

    def link_records_and_serial(record_cls, search):
        for hit in search.scan():
            # Skip linking if the hit doesn't have a legacy recid since it
            # means it's a volume of a multipart
            if "legacy_recid" not in hit:
                continue
            record = record_cls.get_record_by_pid(hit.pid)
            for serial in get_serials_by_child_recid(hit.legacy_recid):
                volume = get_migrated_volume_by_serial_title(
                    record, serial["title"]
                )
                create_parent_child_relation(
                    serial, record, SERIAL_RELATION, volume
                )

    click.echo("Creating serial relations...")
    link_records_and_serial(
        Document, DocumentSearch().filter("term", _migration__has_serial=True)
    )
    link_records_and_serial(
        Series,
        SeriesSearch().filter(
            "bool",
            filter=[
                Q("term", mode_of_issuance="MULTIPART_MONOGRAPH"),
                Q("term", _migration__has_serial=True),
            ],
        ),
    )


def validate_serial_records():
    """Validate that serials were migrated successfully.

    Performs the following checks:
    * Find duplicate serials
    * Ensure all children of migrated serials were migrated
    """

    def validate_serial_relation(serial, recids):
        relations = serial.relations.get().get("serial", [])
        if len(recids) != len(relations):
            click.echo(
                "[Serial {}] Incorrect number of children: {} "
                "(expected {})".format(
                    serial["pid"], len(relations), len(recids)
                )
            )
        for relation in relations:
            child = Document.get_record_by_pid(
                relation["pid"], pid_type=relation["pid_type"]
            )
            if "legacy_recid" in child and child["legacy_recid"] not in recids:
                click.echo(
                    "[Serial {}] Unexpected child with legacy "
                    "recid: {}".format(serial["pid"], child["legacy_recid"])
                )

    titles = set()
    search = SeriesSearch().filter("term", mode_of_issuance="SERIAL")
    for serial_hit in search.scan():
        # Store titles and check for duplicates
        if "title" in serial_hit:
            title = serial_hit.title
            if title in titles:
                current_app.logger.warning(
                    'Serial title "{}" already exists'.format(title)
                )
            else:
                titles.add(title)
        # Check if any children are missing
        children = serial_hit._migration.children
        serial = Series.get_record_by_pid(serial_hit.pid)
        validate_serial_relation(serial, children)

    click.echo("Serial validation check done!")


def validate_multipart_records():
    """Validate that multiparts were migrated successfully.

    Performs the following checks:
    * Ensure all volumes of migrated multiparts were migrated
    """

    def validate_multipart_relation(multipart, volumes):
        relations = multipart.relations.get().get("multipart_monograph", [])
        titles = [volume["title"] for volume in volumes if "title" in volume]
        count = len(set(v["volume"] for v in volumes))
        if count != len(relations):
            click.echo(
                "[Multipart {}] Incorrect number of volumes: {} "
                "(expected {})".format(multipart["pid"], len(relations), count)
            )
        for relation in relations:
            child = Document.get_record_by_pid(
                relation["pid"], pid_type=relation["pid_type"]
            )
            if child["title"] not in titles:
                click.echo(
                    '[Multipart {}] Title "{}" does not exist in '
                    "migration data".format(multipart["pid"], child["title"])
                )

    search = SeriesSearch().filter(
        "term", mode_of_issuance="MULTIPART_MONOGRAPH"
    )
    for multipart_hit in search.scan():
        # Check if any child is missing
        if "volumes" in multipart_hit._migration:
            volumes = multipart_hit._migration.volumes
            multipart = Series.get_record_by_pid(multipart_hit.pid)
            validate_multipart_relation(multipart, volumes)

    click.echo("Multipart validation check done!")
