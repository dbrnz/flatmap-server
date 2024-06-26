#===============================================================================
#
#  Flatmap viewer and annotation tools
#
#  Copyright (c) 2019-2023  David Brooks
#
#  Licensed under the Apache License, Version 2.0 (the "License");
#  you may not use this file except in compliance with the License.
#  You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
#  Unless required by applicable law or agreed to in writing, software
#  distributed under the License is distributed on an "AS IS" BASIS,
#  WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#  See the License for the specific language governing permissions and
#  limitations under the License.
#
#===============================================================================

from datetime import datetime, timezone
from functools import wraps
from pathlib import Path
import json
import os
import sqlite3
from typing import Any, Optional
import uuid

#===============================================================================

import flask            # type: ignore

#===============================================================================

from .pennsieve import get_user
from .server import annotator_blueprint, settings

#===============================================================================
'''
/**
 * A flatmap feature.
 */
export interface MapFeature
{
    id: string
    geometry: {
        type: string
        coordinates: any[]
    }
    properties: Record<any, any>
}

/**
 * Annotation about an item in a resource.
 */
export interface UserAnnotation
{
    resource: string
    item: string
    evidence: URL[]
    comment: string
    feature?: MapFeature
}

interface AnnotationRequest extends UserAnnotation
{
    created: string    // timestamp...
    creator: UserData
}

/**
 * Full annotation about an item in a resource.
 */
export interface Annotation extends AnnotationRequest
{
    id: URL
}

/**
 * Information about a logged in user.
 */
export interface UserData {
    name: string
    email: string
    orcid: string
    canUpdate: boolean
}

TEST_USER = {
    'name': 'Test User',
    'email': 'test@example.org',
    'orcid': '0000-0002-1825-0097',
    'canUpdate': True
}

'''
#===============================================================================

ANNOTATION_STORE_SCHEMA = """
    begin;
    create table annotations (resource text, item text, created text, orcid text, creator text, annotation text);
    create index annotations_index on annotations(resource, item, created, orcid);
    create table features (resource text, item text, deleted text, annotation text, feature text);
    create index features_index on features(resource, item, deleted);
    commit;
"""

PROVENANCE_PROPERTIES = [
    'rdfs:comment',
    'prov:wasDerivedFrom',
]

#===============================================================================

class AnnotationStore:
    def __init__(self, db_path=None):
        if db_path is None:
            db_path = os.path.join(settings['FLATMAP_ROOT'], 'annotation_store.db')
        # Create annotation store if it doesn't exist
        db_name = Path(db_path).resolve()
        if not db_name.exists():
            db = sqlite3.connect(db_name)
            db.executescript(ANNOTATION_STORE_SCHEMA)
            db.close()
        self.__db = sqlite3.connect(db_name)

    def close(self):
    #===============
        if self.__db is not None:
            self.__db.close()
            self.__db = None

    def annotated_items(self, resource_id: str) -> dict:
    #===================================================
        items = []
        if self.__db is not None:
            items = [row[0]
                        for row in self.__db.execute('''select distinct item
                                                        from annotations where resource=?
                                                         order by item''', (resource_id, ))
                                            .fetchall()]
        return {
            'resource': resource_id,
            'items': items
        }

    def user_items(self, resource_id: str, user_id: Optional[str], participated: bool) -> dict:
    #==========================================================================================
        items = []
        if self.__db is not None and user_id is not None:
            # Querying participated annotations if participated True, else non-participated annotations
            items = [row[0]
                        for row in self.__db.execute(f'''select distinct item from annotations
                                                         where resource=? and orcid {"=" if participated else "!="} ?
                                                         order by item''', (resource_id, user_id))
                                            .fetchall()]
        return {
            'resource': resource_id,
            'items': items,
            'userId': user_id,
            'participated': participated,
        }

    def features(self, resource_id: str) -> dict:
    #============================================
        features = []
        if self.__db is not None:
            features = [json.loads(row[0])
                           for row in self.__db.execute('''select feature from features
                                                           where deleted is null and resource=?
                                                           order by item''', (resource_id, ))
                                                .fetchall()]
        return {
            'resource': resource_id,
            'features': features
        }

    def item_features(self, resource_id: str, item_ids: list[str]) -> dict:
    #======================================================================
        features = []
        if self.__db is not None and len(item_ids):
            features = [json.loads(row[0])
                for row in self.__db.execute(f'''select feature from features
                                                 where deleted is null and resource=?
                                                       and item in ({", ".join("?"*len(item_ids))})
                                                 order by item''', (resource_id, *item_ids))
                                    .fetchall()]
        return {
            'resource': resource_id,
            'features': features
        }

    def annotations(self, resource_id: Optional[str]=None, item_id: Optional[str]=None) -> list[dict]:
    #=================================================================================================
        annotations = []
        if self.__db is not None:
            where_values = []
            if resource_id is None:
                where_statement =  ''
            else:
                where_clauses = ['resource=?']
                where_values.append(resource_id)
                if item_id is not None:
                    where_clauses.append('item=?')
                    where_values.append(item_id)
                where_statement = 'where ' + ' and '.join(where_clauses)
            for row in self.__db.execute(f'''select rowid, created, creator, annotation, resource, item
                                        from annotations {where_statement}
                                        order by created desc, creator''',
                                    tuple(where_values)).fetchall():
                annotation = {
                    'annotationId': int(row[0]),
                    'resource': row[4],
                    'item': row[5],
                    'created': row[1],
                    'creator': json.loads(row[2])
                }
                annotation.update(json.loads(row[3]))
                annotations.append(annotation)
        return annotations

    def annotation(self, annotation_id: int) -> dict:
    #================================================
        annotation = {}
        if self.__db is not None:
            row = self.__db.execute('''select a.resource, a.item, a.created, a.creator, a.annotation, f.feature
                                        from annotations as a left join features as f on a.rowid = f.annotation
                                        where a.rowid=? and f.deleted is null''', (annotation_id, )).fetchone()
            if row is not None:
                annotation = {
                    'annotationId': int(annotation_id),
                    'resource': row[0],
                    'item': row[1],
                    'created': row[2],
                    'creator': json.loads(row[3]),
                    'feature': json.loads(row[5]) if row[5] else None,
                }
                annotation.update(json.loads(row[4]))
        return annotation

    def add_annotation(self, annotation: dict) -> dict:
    #==================================================
        result = {}
        if self.__db is not None:
            created = annotation.pop('created', None)
            if created is None:
                created = datetime.now(tz=timezone.utc).isoformat(timespec='seconds')
            creator = annotation.pop('creator', None)
            resource_id = annotation.pop('resource', None)
            item_id = annotation.pop('item', None)
            if (resource_id and item_id
            and creator and (orcid := creator.get('orcid'))):
                creator.pop('canUpdate', None)
                try:
                    feature = annotation.pop('feature', None)
                    cursor = self.__db.cursor()
                    cursor.execute('''insert into annotations
                        (resource, item, created, orcid, creator, annotation) values (?, ?, ?, ?, ?, ?)''',
                        (resource_id, item_id, created, orcid, json.dumps(creator), json.dumps(annotation)))
                    if cursor.lastrowid is not None:
                        result['annotationId'] = int(cursor.lastrowid)
                    # Flag as deleted any non-deleted entries for the feature
                    cursor.execute('''update features set deleted=?
                        where deleted is null and resource=? and item=?''',
                        (result['annotationId'], resource_id, item_id))
                    if feature and isinstance(feature, dict):
                        # Add a new row when we have a new feature
                        cursor.execute('''insert into features
                            (resource, item, annotation, deleted, feature) values (?, ?, ?, null, ?)''',
                            (resource_id, item_id, result['annotationId'], json.dumps(feature)))
                    cursor.execute('commit')
                except sqlite3.OperationalError as err:
                    result['error'] = str(err)
        else:
            result['error'] = 'No annotation database...'
        return result

#===============================================================================

__sessions: dict[str, dict] = {}

def __session_key(key: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, key))

def __new_session(key: str, data: dict) -> str:
    session_key = __session_key(key)
    __sessions[session_key] = data
    return session_key

def __session_data(session_key: str) -> Optional[dict]:
    return __sessions.get(session_key)

def __del_session(session_key: str) -> bool:
    return __sessions.pop(session_key, None) is not None

#===============================================================================

def __authenticated(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if flask.request.method == 'GET':
            parameters = flask.request.args
        elif flask.request.method == 'POST':
            parameters = flask.request.get_json()
        else:
            parameters = {}
        if ((key := parameters.get('key')) is not None
          and (session_key := parameters.get('session')) is not None
          and session_key == __session_key(key)
          and (data := __session_data(session_key)) is not None):
            flask.g.update = data.get('canUpdate', False)
            return f(*args, **kwargs)
        response = flask.make_response('{"error": "forbidden"}', 403)
        return response
    return decorated_function

#===============================================================================

def __get_parameter(name: str, default: Any=None):
    value = flask.request.args.get(name)
    result = json.loads(value) if value is not None else default
    return result

#===============================================================================

@annotator_blueprint.route('authenticate', methods=['GET'])
def authenticate():
    parameters = flask.request.args
    if (key := parameters.get('key')) is not None:
        user_data = get_user(key)
    else:
        user_data = {'error': 'forbidden'}
    if 'error' not in user_data:
        session_key = __new_session(key, user_data)
        response = flask.make_response(json.dumps({
            'session': session_key,
            'data': user_data
        }))
    else:
        response = flask.make_response(json.dumps(user_data), 403)
    response.mimetype = 'application/json'
    return response

#===============================================================================

@annotator_blueprint.route('unauthenticate', methods=['GET'])
def unauthenticate():
    response = flask.make_response('{"success": "Unauthenticated"}')
    response.mimetype = 'application/json'
    return response

#===============================================================================

@annotator_blueprint.route('items/', methods=['GET'])
@__authenticated
def annotated_items():
    resource_id = __get_parameter('resource')
    user_id = __get_parameter('user')
    annotation_store = AnnotationStore()
    if user_id is not None:
        participated = __get_parameter('participated', True)
        items = annotation_store.user_items(resource_id, user_id, participated)
    else:
        items = annotation_store.annotated_items(resource_id)
    annotation_store.close()
    return flask.jsonify(items)

#===============================================================================

@annotator_blueprint.route('features/', methods=['GET'])
@__authenticated
def features():
    resource_id = __get_parameter('resource')
    items = __get_parameter('items')
    annotation_store = AnnotationStore()
    if items is not None:
        if isinstance(items, str):
            items = [items]
        features = annotation_store.item_features(resource_id, items)
    else:
        features = annotation_store.features(resource_id)
    annotation_store.close()
    return flask.jsonify(features)

#===============================================================================

@annotator_blueprint.route('annotations/', methods=['GET'])
@__authenticated
def annotations():
    resource_id = __get_parameter('resource')
    item_id = __get_parameter('item')
    annotation_store = AnnotationStore()
    items = annotation_store.annotations(resource_id, item_id)
    annotation_store.close()
    return flask.jsonify(items)

#===============================================================================

@annotator_blueprint.route('annotation/', methods=['GET'])
@__authenticated
def annotation():
    annotation_id = __get_parameter('annotation')
    annotation_store = AnnotationStore()
    annotation = annotation_store.annotation(annotation_id)
    annotation_store.close()
    return flask.jsonify(annotation)

#===============================================================================

@annotator_blueprint.route('annotation/', methods=['POST'])
@__authenticated
def add_annotation():
    annotation_store = AnnotationStore()
    if flask.request.method == 'POST' and flask.g.update:
        annotation = flask.request.get_json().get('data', {})
        result = annotation_store.add_annotation(annotation)
    else:
        result = '{"error": "forbidden"}', 403, {'mimetype': 'application/json'}
    annotation_store.close()
    return flask.jsonify(result)

#===============================================================================
#===============================================================================

@annotator_blueprint.route('download/', methods=['GET'])
def download():
    annotation_store = AnnotationStore()
    annotations = annotation_store.annotations()
    annotation_store.close()
    return flask.jsonify(annotations)

#===============================================================================
#===============================================================================
