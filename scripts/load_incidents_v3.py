#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""Loads incidents from multiple incident database dumps (schema v3)"""
from contextlib import contextmanager
from itertools import groupby
from collections import defaultdict
from datetime import datetime, timedelta
import argparse
import csv
from dateutil import parser
import logging
import json
import os
import pytz
from time import sleep
import uuid

import requests

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger()


@contextmanager
def open_many(file_paths, mode='r'):
    # Allows opening arbitrary number of files in a single context manager
    handlers = {path: open(path, mode) for path in file_paths}
    try:
        yield handlers
    finally:
        for fp in handlers.values():
            fp.close()


def merge_sort_files(paths, join_col):
    # Merges multiple inputs into a stream of ordered tuples of the format (id, source, row)
    with open_many(paths, 'r') as handlers:
        readers = {key: csv.DictReader(fp) for key, fp in handlers.items()}

        lines = {}
        for key, reader in readers.items():
            try:
                lines[key] = next(reader)
            except StopIteration:
                # If the file has no rows, ignore it
                pass
        # Loop over all open files until we've removed them all
        while lines:
            # Find the lowest ID in any of the open files
            join_id = min(line[join_col] for line in lines.values())
            for key in lines.keys():
                line = lines[key]
                while line[join_col] == join_id:
                    # If the ID matches, send it up and then read the next line from the file
                    yield (join_id, key, line)
                    try:
                        line = next(readers[key])
                        lines[key] = line
                    except StopIteration:
                        # We've reached the end of this file, remove the file from consideration
                        del lines[key]
                        break


def collate_multiple_files(paths, join_col):
    merged_stream = merge_sort_files(paths, join_col)

    # Merge the sorted stream of rows into dictionaries per unique ID
    for id, matches in groupby(merged_stream, lambda row: row[0]):
        result = defaultdict(list)
        for id, key, line in matches:
            result[key].append(line)
        yield result


def extract(csv_path):
    """Simply pulls rows into a DictReader"""
    with open(csv_path) as csvfile:
        reader = csv.DictReader(csvfile, delimiter=',')
        for row in reader:
            yield row


def format_record_object(data, mapping):
    output = {}
    for csv_key, driver_key, cast_func in mapping:
        try:
            value = data[csv_key]
        except KeyError:
            continue
        output[driver_key] = cast_func(value)

    # Add in the _localId field; they're not used here but the schema requires them
    output['_localId'] = str(uuid.uuid4())

    return output


def construct_record_data(record, persons, vehicles):
    return {
        'driverIncidentDetails': format_record_object(record, [
            ('', 'Log1', int),
            ('', 'Numero', int),
            ('', 'CodReferencia', int),
            ('', 'Log2', int),
            ('', 'Log3', int),
            ('', 'CodIntersecao', int),
            ('', 'Jurisdicao', str),
            ('', 'CodNatureza', int),
            ('', 'TipoCruzamento', int),
            ('', 'INTERSEÇÃO?', str),
            ('', 'Natureza', str)
        ]),
        'driverPerson': [format_record_object(person,  [
            ('', 'CdPessoa', int),
            ('', 'CdGravidadeLesao', int),
            ('', 'Sexo', str),
            ('', 'TipoPessoa', int),
            ('', 'CdVeiculo', int),
            ('', 'Idade', int)
        ]) for person in persons],
        'driverVehicle': [format_record_object(vehicle,  [
            ('', 'CdVeiculo', int),
            ('', 'Ano', int),
            ('', 'TipoVeiculo', str),
            ('', 'Linha', int)
        ]) for vehicle in vehicles]
    }


def transform(record, vehicles, people, schema_id):
    """Converts denormalized rows into objects compliant with the schema.

    Doesn't do anything fancy -- if the schema changes, this needs to change too.
    """

    # Calculate value for the occurred_from/to fields in local time
    occurred_date = parser.parse('{date} {time}'.format(
        date=record['Data'],
        time=record['Hora'] if record['Hora'] != 'N' else ''
    ))
    occurred_date = pytz.timezone('America/Sao_Paulo').localize(occurred_date)

    # Set the geom field
    geom = "POINT ({lon} {lat})".format(
        lon=float(record['Longitude']),
        lat=float(record['Latitude'])
    )

    obj = {
        'data': construct_record_data(record, vehicles, people),
        'schema': str(schema_id),
        'occurred_from': occurred_date.isoformat(),
        'occurred_to': occurred_date.isoformat(),
        'geom': geom
    }

    return obj


def load(obj, api, headers=None):
    """Load a transformed object into the data store via the API"""
    if headers is None:
        headers = {}

    url = api + '/records/'
    data = json.dumps(obj)
    headers = dict(headers)
    headers.setdefault('content-type', 'application/json')

    while True:
        response = requests.post(url, data=data, headers=headers)
        sleep(0.2)
        if response.status_code == 201:
            return
        else:
            logger.error(response.text)
            logger.error('retrying...')


def create_schema(schema_path, api, headers=None):
    """Create a recordtype/schema into which to load all new objects"""
    # Create record type
    response = requests.post(api + '/recordtypes/',
                             data={'label': 'Incident',
                                   'plural_label': 'Incidents',
                                   'description': 'Historical incident data',
                                   'temporal': True,
                                   'active': True},
                             headers=headers)
    response.raise_for_status()
    rectype_id = response.json()['uuid']
    logger.info('Created RecordType')
    # Create associated schema
    with open(schema_path, 'r') as schema_file:
        schema_json = json.load(schema_file)
        response = requests.post(api + '/recordschemas/',
                                 data=json.dumps({u'record_type': rectype_id,
                                                  u'schema': schema_json}),
                                 headers=dict({'content-type': 'application/json'}.items() +
                                              headers.items()))
    logger.debug(response.json())
    response.raise_for_status()
    logger.info('Created RecordSchema')
    return response.json()['uuid']


def main():
    parser = argparse.ArgumentParser(description='Load incidents data (v3)')
    parser.add_argument('incidents_csv_dir', help='Path to directory containing incidents CSVs')
    parser.add_argument('--schema-path', help='Path to JSON file defining schema',
                        default=os.path.join(os.path.dirname(os.path.realpath(__file__)),
                                             'incident_schema_v3.json'))
    parser.add_argument('--api-url', help='API host / path to target for loading data',
                        default='http://localhost:7000/api')
    parser.add_argument('--authz', help='Authorization header')
    parser.add_argument('--schema-id', help='UUID for the Record Type to use')
    args = parser.parse_args()

    headers = None

    if args.authz:
        headers = {'Authorization': args.authz}

    # Do the work
    schema_id = args.schema_id
    if not schema_id:
        logger.info("Creating schema remotely")
        schema_id = create_schema(args.schema_path, args.api_url, headers)
    logger.info("Loading data")

    # Load all files in the directory, ordered by file size
    files = {
        'record': 'acidentes.csv',
        'vehicles': 'veiculos.csv',
        'people': 'vitimas.csv'
    }

    logger.info("{} - Importing records".format(datetime.now()))
    last_print = datetime.now()

    count = 0
    for record_set in collate_multiple_files(files.values(), 'CdAcidente'):
        if datetime.now() - last_print > timedelta(minutes=1):
            logger.info("{} - Imported {} records".format(datetime.now(), count))
            last_print = datetime.now()

        try:
            record = record_set[files['record']][0]
        except KeyError:
            # Somehow there was a record in one of the addendum files that wasn't in the main file
            # We don't have enough info to go on, so log the error and skip it
            logger.warn("Found record join with no associated incident, skipping")
            continue

        vehicles = record_set.get(files['vehicles'], [])
        people = record_set.get(files['people'], [])

        record_data = transform(record, vehicles, people, schema_id)
        print(json.dumps(record_data))
        continue

        load(record_data, args.api_url, headers)
        count += 1
    logger.info('Loading complete')


if __name__ == '__main__':
    main()
