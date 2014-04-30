from __future__ import absolute_import

from django.db import models
from django.conf import settings

from elasticsearch import Elasticsearch, NotFoundError, RequestError
from elasticsearch.helpers import bulk

from wagtail.wagtailsearch.backends.base import BaseSearch
from wagtail.wagtailsearch.indexed import Indexed

import string
import datetime


class FilterError(Exception):
    pass


class ElasticSearchQuery(object):
    def __init__(self, query_set, query_string, fields=None, _es_type=None):
        self.query_set = query_set
        self.query_string = query_string
        self.fields = fields

        if _es_type is not None:
            self._es_type = _es_type
        else:
            self._es_type = ElasticSearchType(self.query_set.model)

    def _clone(self):
        klass = self.__class__
        return klass(self.query_set, self.query_string, fields=self.fields, _es_type=self._es_type)

    def _get_filters_from_where(self, where_node):
        # Check if this is a leaf node
        if isinstance(where_node, tuple):
            field =  self._es_type.get_filter_field_name(where_node[0].col)
            lookup = where_node[1]
            value = where_node[3]

            es_field = self._es_type.get_field(field)

            # Find lookup
            if lookup == 'exact':
                if value is None:
                    return {
                        'missing': {
                            'field': field,
                        }
                    }
                else:
                    return {
                        'term': {
                            field: es_field.convert_value(value)
                        }
                    }

            if lookup == 'isnull':
                if value:
                    return {
                        'missing': {
                            'field': field,
                        }
                    }
                else:
                    return {
                        'not': {
                            'missing': {
                                'field': field,
                            }
                        }
                    }

            if lookup in ['startswith', 'prefix']:
                return {
                    'prefix': {
                        field: es_field.convert_value(value)
                    }
                }

            if lookup in ['gt', 'gte', 'lt', 'lte']:
                return {
                    'range': {
                        field: {
                            lookup: es_field.convert_value(value),
                        }
                    }
                }

            if lookup == 'range':
                lower, upper = value

                return {
                    'range': {
                        field: {
                            'gte': es_field.convert_value(lower),
                            'lte': es_field.convert_value(upper),
                        }
                    }
                }

            raise FilterError('Could not apply filter on ElasticSearch results "' + field + '__' + lookup + ' = ' + str(value) + '". Lookup "' + lookup + '"" not recognosed.')

        # Get child filters
        connector = where_node.connector
        child_filters = [self._get_filters_from_where(child) for child in where_node.children]
        child_filters = [child_filter for child_filter in child_filters if child_filter]

        # Connect them
        if child_filters:
            if len(child_filters) == 1:
                filter_out = child_filters[0]
            else:
                filter_out = {
                    connector.lower(): [
                        fil for fil in child_filters if fil is not None
                    ]
                }

            if where_node.negated:
                filter_out = {
                    'not': filter_out
                }

            return filter_out

    def _get_filters(self):
        # Filters
        filters = []

        # Filter by content type
        filters.append({
            'prefix': {
                'content_type': self.query_set.model._get_qualified_content_type_name()
            }
        })

        # Apply filters from queryset
        query_set_filters = self._get_filters_from_where(self.query_set.query.where)
        if query_set_filters:
            filters.append(query_set_filters)

        return filters

    def to_es(self):
        # Query
        query = {
            'query_string': {
                'query': self.query_string,
            }
        }

        # Fields
        if self.fields:
            query['query_string']['fields'] = self.fields

        # Filters
        filters = self._get_filters()
        if len(filters) == 1:
            query = {
                'filtered': {
                    'query': query,
                    'filter': filters[0],
                }
            }
        elif len(filters) > 1:
            query = {
                'filtered': {
                    'query': query,
                    'filter': {
                        'and': filters,
                    }
                }
            }

        return query


class ElasticSearchResults(object):
    def __init__(self, backend, query):
        self.backend = backend
        self.query = query
        self.start = 0
        self.stop = None
        self._results_cache = None
        self._hit_count = None

    def _clone(self):
        klass = self.__class__
        new = klass(self.backend, self.query._clone())
        new.start = self.start
        new.stop = self.stop
        return new

    def set_limits(self, start=None, stop=None):
        if stop is not None:
            if self.stop is not None:
                self.stop = min(self.stop, self.start + stop)
            else:
                self.stop = self.start + stop

        if start is not None:
            if self.stop is not None:
                self.start = min(self.stop, self.start + start)
            else:
                self.start = self.start + start

    def _get_pks(self):
        # Get query
        query = self.query.to_es()

        # Params for elasticsearch query
        params = dict(
            index=self.backend.es_index,
            body=dict(query=query),
            _source=False,
            fields='pk',
            from_=self.start,
        )

        # Add size if set
        if self.stop is not None:
            params['size'] = self.stop - self.start

        # Send to ElasticSearch
        hits = self.backend.es.search(**params)

        # Get pks from results
        pks = [hit['fields']['pk'] for hit in hits['hits']['hits']]

        # ElasticSearch 1.x likes to pack pks into lists, unpack them if this has happened
        return [pk[0] if isinstance(pk, list) else pk for pk in pks]

    def _do_count(self):
        query = self.query.to_es()

        # Elasticsearch 1.x
        count = self.backend.es.count(
            index=self.backend.es_index,
            body=dict(query=query),
        )

        # ElasticSearch 0.90.x fallback
        if not count['_shards']['successful'] and "No query registered for [query]]" in count['_shards']['failures'][0]['reason']:
            count = self.backend.es.count(
                index=self.backend.es_index,
                body=query,
            )

        # Get count
        hit_count = count['count']

        # Add limits
        hit_count -= self.start
        if self.stop is not None:
            hit_count = min(hit_count, self.stop - self.start)

        return max(hit_count, 0)

    def count(self):
        if self._hit_count is None:
            if self._results_cache is not None:
                self._hit_count = len(self._results_cache)
            else:
                self._hit_count = self._do_count()
        return self._hit_count

    def _do_search(self):
        # Get list of PKs from ElasticSearch
        pks = self._get_pks()

        # Initialise results dictionary
        results = dict((str(pk), None) for pk in pks)

        # Find objects in database and add them to dict
        query_set = self.query.query_set.filter(pk__in=pks)
        for obj in query_set:
            results[str(obj.pk)] = obj

        # Return results in order given by ElasticSearch
        return [results[str(pk)] for pk in pks if results[str(pk)]]

    def _fetch_all(self):
        if self._results_cache is None:
            self._results_cache = self._do_search()
        return self._results_cache

    def __getitem__(self, key):
        new = self._clone()

        if isinstance(key, slice):
            # Set limits
            start = int(key.start) if key.start else None
            stop = int(key.stop) if key.stop else None
            new.set_limits(start, stop)

            # Copy results cache
            if self._results_cache is not None:
                new._results_cache = self._results_cache[key]

            return new
        else:
            # Return a single item
            if self._results_cache is not None:
                return self._results_cache[key]

            new.start = key
            new.stop = key + 1
            return list(new)[0]
  
    def __repr__(self):
        data = list(self[:21])
        if len(data) > 20:
            data[-1] = "...(remaining elements truncated)..."
        return repr(data)

    def __iter__(self):
        return iter(self._fetch_all())
  
    def __len__(self):
        return len(self._fetch_all())


class ElasticSearchField(object):
    IGNORED_TYPES = ['FileField']
    TYPE_MAP = {
        'TextField': 'string',
        'SlugField': 'string',
        'CharField': 'string',
        'PositiveIntegerField': 'integer',
        'BooleanField': 'boolean',
        'OneToOneField': 'string',
        'ForeignKey': 'string',
        'AutoField': 'integer',
        'DateField': 'date',
        'TimeField': 'date',
        'DateTimeField': 'date',
        'IntegerField': 'integer',
    }

    def __init__(self, **params):
        # Copy params to prevent us accidentally trashing something important
        self.params = params.copy()

        # Field type
        if 'type' in self.params:
            self.type = self.params['type']
            del self.params['type']
        elif 'django_type' in self.params:
            self.type = self.convert_type(self.params['django_type'])
            del self.params['django_type']
        else:
            self.type = 'string'

    def can_be_indexed(self):
        return self.type is not None

    def convert_type(self, django_type):
        # Skip if in ignored types
        if django_type in self.IGNORED_TYPES:
            return

        # Lookup es type from TYPE_MAP
        if django_type in self.TYPE_MAP:
            return self.TYPE_MAP[django_type]

    def convert_value(self, value):
        if value is None:
            return

        if self.type == 'string':
            return unicode(value)
        elif self.type == 'integer':
            return int(value)
        elif self.type == 'boolean':
            return bool(value)
        elif self.type == 'date':
            # Does it quack like a datetime?
            # If not, crash.
            return value.isoformat()

    def get_mapping(self):
        mapping = {
            'type': self.type
        }
        mapping.update(self.params)
        return mapping


class ElasticSearchType(object):
    FILTER_FIELD_SUFFIX = '_val'

    def get_filter_field_name(self, name):
        return name + self.FILTER_FIELD_SUFFIX

    def __init__(self, model):
        self.model = model
        self._fields = None

    def get_doc_type(self):
        return self.model._get_qualified_content_type_name()

    def _get_fields(self):
        # Get field list
        filterable_fields = self.model._get_filterable_fields().items()
        searchable_fields = self.model._get_searchable_fields().items()

        # Suffix filterable_fields
        filterable_fields = [(self.get_filter_field_name(field), config) for field, config in filterable_fields]

        # Build ES fields
        fields = [
            (name, ElasticSearchField(**config))
            for name, config in filterable_fields + searchable_fields
        ]

        # Remove fields that can't be indexed
        fields = [(name, field) for name, field in fields if field.can_be_indexed()]

        # Return
        return dict(fields)

    def get_fields(self):
        # Do some caching to prevent having to keep building the field list
        if self._fields is None:
            self._fields = self._get_fields()
        return self._fields

    def get_field(self, name):
        return self.get_fields()[name]

    def has_field(self, name):
        return name in self.get_fields()

    def build_mapping(self):
        # Make field list
        fields = {
            'pk': {
                'type': 'string',
                'index': 'not_analyzed',
                'store': 'yes',
            },
            'content_type': {
                'type': 'string',
                'index': 'not_analyzed',
            },
        }
        fields.update([(name, field.get_mapping()) for name, field in self.get_fields().items()])

        return {
            self.get_doc_type(): {
                'properties': fields,
            }
        }


class ElasticSearchDocument(object):
    def __init__(self, obj):
        self.obj = obj
        self.es_type = ElasticSearchType(obj.__class__)

    def get_id(self):
        return self.obj._get_base_content_type_name() + ':' + str(self.obj.pk)

    def build_document(self):
        # Build document
        doc = {
            'pk': str(self.obj.pk),
            'content_type': self.obj._get_qualified_content_type_name(),
            'id': self.get_id(),
        }

        # Add filterable fields
        filterable_fields = self.obj._get_filterable_fields()
        for field in filterable_fields.keys():
            filter_field = self.es_type.get_filter_field_name(field)
            if hasattr(self.obj, field) and self.es_type.has_field(filter_field):
                # Get field value
                value = getattr(self.obj, field)

                # Convert value
                value = self.es_type.get_field(filter_field).convert_value(value)

                # Add to document
                doc[filter_field] = value

        # Add searchable fields
        searchable_fields = self.obj._get_searchable_fields()
        for field in searchable_fields.keys():
            if hasattr(self.obj, field) and self.es_type.has_field(field):
                # Get field value
                value = getattr(self.obj, field)

                # Check if this field is callable
                if hasattr(value, '__call__'):
                    # Call it
                    value = value()

                # Convert value
                value = self.es_type.get_field(field).convert_value(value)

                # Add to document
                doc[field] = value

        return doc


class ElasticSearch(BaseSearch):
    def __init__(self, params):
        super(ElasticSearch, self).__init__(params)

        # Get settings
        self.es_urls = params.get('URLS', ['http://localhost:9200'])
        self.es_index = params.get('INDEX', 'wagtail')

        # Get ElasticSearch interface
        self.es = Elasticsearch(urls=self.es_urls)

    def reset_index(self):
        # Delete old index
        try:
            self.es.indices.delete(self.es_index)
        except NotFoundError:
            pass

        # Settings
        INDEX_SETTINGS = {
            'settings': {
                'analysis': {
                    'analyzer': {
                        'ngram_analyzer': {
                            'type': 'custom',
                            'tokenizer': 'lowercase',
                            'filter': ['ngram']
                        },
                        'edgengram_analyzer': {
                            'type': 'custom',
                            'tokenizer': 'lowercase',
                            'filter': ['edgengram']
                        }
                    },
                    'tokenizer': {
                        'ngram_tokenizer': {
                            'type': 'nGram',
                            'min_gram': 3,
                            'max_gram': 15,
                        },
                        'edgengram_tokenizer': {
                            'type': 'edgeNGram',
                            'min_gram': 2,
                            'max_gram': 15,
                            'side': 'front'
                        }
                    },
                    'filter': {
                        'ngram': {
                            'type': 'nGram',
                            'min_gram': 3,
                            'max_gram': 15
                        },
                        'edgengram': {
                            'type': 'edgeNGram',
                            'min_gram': 1,
                            'max_gram': 15
                        }
                    }
                }
            }
        }

        # Create new index
        self.es.indices.create(self.es_index, INDEX_SETTINGS)

    def add_type(self, model):
        # Get ElasticSearchType object for this model
        es_type = ElasticSearchType(model)

        # Put mapping
        self.es.indices.put_mapping(
            index=self.es_index,
            doc_type=es_type.get_doc_type(),
            body=es_type.build_mapping()
        )

    def refresh_index(self):
        self.es.indices.refresh(self.es_index)

    def add(self, obj):
        # Make sure the object can be indexed
        if not self.object_can_be_indexed(obj):
            return

        # Get document
        es_doc = ElasticSearchDocument(obj)

        # Add to index
        self.es.index(
            self.es_index,
            es_doc.es_type.get_doc_type(),
            es_doc.build_document(),
            id=es_doc.get_id()
        )

    def add_bulk(self, obj_list):
        # Group all objects by their type
        type_set = {}
        for obj in obj_list:
            # Object must be a decendant of Indexed and be a django model
            if not self.object_can_be_indexed(obj):
                continue

            # Get object type
            obj_type = obj._get_qualified_content_type_name()

            # If type is currently not in set, add it
            if obj_type not in type_set:
                type_set[obj_type] = []

            # Add object to set
            type_set[obj_type].append(ElasticSearchDocument(obj))

        # Loop through each type and bulk add them
        for type_name, es_docs in type_set.items():
            # Get list of actions
            actions = []
            for es_doc in es_docs:
                action = {
                    '_index': self.es_index,
                    '_type': type_name,
                    '_id': es_doc.get_id(),
                }
                action.update(es_doc.build_document())
                actions.append(action)

            yield type_name, len(es_docs)
            bulk(self.es, actions)

    def delete(self, obj):
        # Object must be a decendant of Indexed and be a django model
        if not isinstance(obj, Indexed) or not isinstance(obj, models.Model):
            return

        # Delete document
        es_doc = ElasticSearchDocument(obj)
        try:
            self.es.delete(
                self.es_index,
                es_doc.es_type.get_doc_type(),
                es_doc.get_id(),
            )
        except NotFoundError:
            pass  # Document doesn't exist, ignore this exception

    def search(self, query_set, query_string, fields=None):
        # Model must be a descendant of Indexed
        if not issubclass(query_set.model, Indexed):
            return query_set.none()

        # Clean up query string
        query_string = "".join([c for c in query_string if c not in string.punctuation])

        # Check that theres still a query string after the clean up
        if not query_string:
            return query_set.none()

        # Get fields
        if fields is None:
            fields = query_set.model._get_searchable_fields().keys()

        # Return nothing if there are no fields
        if not fields:
            return query_set.none()

        # Return search results
        return ElasticSearchResults(self, ElasticSearchQuery(query_set, query_string, fields=fields))
