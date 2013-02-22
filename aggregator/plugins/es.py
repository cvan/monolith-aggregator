from collections import defaultdict
import datetime

from pyelasticsearch import ElasticHttpError
from pyelasticsearch import ElasticHttpNotFoundError
from pyelasticsearch import ElasticSearch
from pyelasticsearch.client import es_kwargs

from aggregator.plugins import Plugin
from aggregator.util import urlsafe_uuid


class ExtendedClient(ElasticSearch):
    """Wrapper around pyelasticsearch's client to add some missing
    API's. These should be merged upstream.
    """

    @es_kwargs()
    def create_template(self, name, settings, query_params=None):
        """
        Create an index template.

        :arg name: The name of the template.
        :arg settings: A dictionary of settings.

        See `ES's index-template API`_ for more detail.

        .. _`ES's index-template API`:
           http://tinyurl.com/es-index-template
        """
        return self.send_request('PUT', ['_template', name], settings,
                                 query_params=query_params)

    @es_kwargs()
    def delete_template(self, name, query_params=None):
        """
        Delete an index template.

        :arg name: The name of the template.

        See `ES's index-template API`_ for more detail.

        .. _`ES's index-template API`:
            http://tinyurl.com/es-index-template
        """
        return self.send_request('DELETE', ['_template', name],
                                 query_params=query_params)

    @es_kwargs()
    def get_template(self, name, query_params=None):
        """
        Get the settings of an index template.

        :arg name: The name of the template.

        See `ES's index-template API`_ for more detail.

        .. _`ES's index-template API`:
            http://tinyurl.com/es-index-template
        """
        return self.send_request('GET', ['_template', name],
                                 query_params=query_params)

    def list_templates(self):
        """
        Get a dictionary with all index template settings.

        See `ES's index-template API`_ for more detail.

        .. _`ES's index-template API`:
            http://tinyurl.com/es-index-template
        """
        res = self.cluster_state(filter_routing_table=True,
                                 filter_nodes=True, filter_blocks=True)
        return res['metadata']['templates']

    @es_kwargs('filter_nodes', 'filter_routing_table', 'filter_metadata',
               'filter_blocks', 'filter_indices')
    def cluster_state(self, query_params=None):
        """
        The cluster state API allows to get a comprehensive state
        information of the whole cluster.

        :arg query_params: A map of querystring param names to values or
            ``None``

        See `ES's cluster-state API`_ for more detail.

        .. _`ES's cluster-state API`:
           http://tinyurl.com/cluster-state
        """
        return self.send_request(
            'GET', ['_cluster', 'state'], query_params=query_params)

    @es_kwargs()
    def multi_get(self, index=None, doc_type=None, body=None,
                  query_params=None):
        if not body:
            # keep index and doc_type as first arguments,
            # but require body
            raise ValueError('A body is required.')

        return self.send_request(
            'GET',
            [self._concat(index), self._concat(doc_type), '_mget'],
            body,
            query_params=query_params)


class ESSetup(object):

    def __init__(self, client):
        self.client = client

    def _default_settings(self):
        return {
            "settings": {
                "refresh_interval": "10s",
                "default_field": "_id",
                "analysis": {
                    "analyzer": {
                        "default": {
                            "type": "custom",
                            "tokenizer": "keyword",
                        },
                    },
                },
                "store": {
                    "compress": {
                        "stored": "true",
                        "tv": "true",
                    },
                },
                "cache": {
                    "field": {
                        "type": "soft",
                    },
                },
            },
            "mappings": {
                "_default_": {
                    "_all": {"enabled": False},
                    "dynamic_templates": [{
                        "disable_string_analyzing": {
                            "match": "*",
                            "match_mapping_type": "string",
                            "mapping": {
                                "type": "string",
                                "index": "not_analyzed",
                            },
                        },
                    }],
                },
            },
        }

    def configure_templates(self):
        # setup template for time-slice index
        res = self.client.get_template("time_1")
        if res:  # pragma: no cover
            try:
                self.client.delete_template("time_1")
            except Exception:
                pass
        time_settings = self._default_settings()
        time_settings["template"] = "time_*"
        time_settings["settings"]["number_of_shards"] = 1
        time_settings["settings"]["number_of_replicas"] = 1
        self.client.create_template("time_1", time_settings)

    def optimize_index(self, name):
        """Fully optimize an index down to one segment.
        """
        return self.client.optimize(
            name, max_num_segments=1, wait_for_merge=True)


class ESWrite(Plugin):

    def __init__(self, **options):
        self.options = options
        self.url = options['url']
        self.client = ExtendedClient(self.url)
        self.setup = ESSetup(self.client)
        self.setup.configure_templates()

    def _index_name(self, date):
        return 'time_%.4d-%.2d' % (date.year, date.month)

    def _bulk_index(self, index, doc_type, docs, id_field='id'):
        # an optimized version of the bulk_index, avoiding
        # repetition of index and doc_type in each action line
        _encode_json = self.client._encode_json
        body_bits = []
        for doc in docs:
            id_ = doc.pop(id_field, urlsafe_uuid(doc.get('date', None)))
            action = {'index': {'_id': id_}}
            body_bits.extend([_encode_json(action), _encode_json(doc)])

        # Need the trailing newline.
        body = '\n'.join(body_bits) + '\n'
        return self.client.send_request('POST',
                                        [index, doc_type, '_bulk'],
                                        body,
                                        encode_body=False)

    def __call__(self, batch):
        holder = defaultdict(list)
        today = datetime.date.today()

        # sort data into index/type buckets
        for source_id, item in batch:
            # XXX use source_id as a key with dates for updates
            item = dict(item)
            date = item.get('date', today)
            index = self._index_name(date)
            category = item.pop('category', 'unknown')
            holder[(index, category)].append(item)

        # submit one bulk request per index/type combination
        for key, docs in holder.items():
            self._bulk_index(key[0], key[1], docs, id_field='uid')
