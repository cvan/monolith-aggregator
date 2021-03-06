Monolith ElasticSearch
======================

Notes on configuration and setup of ElasticSearch for monolith.

Assumptions
:::::::::::

In order to gain performance, we can lower precision and latency for a lot
of the metrics. But looking forward, being near real-time and high precision
would still be nice.

Currently external systems do some of the aggregation, but in the future we
might want to do this work inside monolith to gain more real-time and
precision.

Time-series indexes
:::::::::::::::::::

Multiple indexes are created to cover a distinct time period each. The
best time unit depends on the exact data volume and retention policies.
High-volume log/event setups use daily indexes, but that's likely complete
overkill for our concerns.

Currently all stats are kept in a single (sharded) index with roughly 45gb
of raw data in total.

The suggested starting setup is to use monthly indexes. Old months can
then be easily archived / rolled up into lower granularity (1 minute / daily /
weekly etc. time precision). So there would be indexes like::

    time_2013-01
    time_2012-12
    time_2012-11
    time_2012-10

All writes would happen to the *correct* index based on the timestamp of each
entry.

In addition each index can be split up into multiple shards, to distribute load
across different servers. At first a shard size of 1 is used, so each shard
holds roughly one month of data. Typical queries are for the last 30 days, so
usually involve queries for the current and former month. In this setup that's
querying two index shards.

Replication isn't needed to gain protection against data-loss, as ES isn't the
primary persistent storage. We'd still use a replication factor of 1 (meaning
there's two copies of all data) to spread read-load across multiple servers.
Depending on the load, we could increase the shard count for the current and
last month, as these are likely queried a lot more often than the older data.

**Note** These index/shard settings are aimed to keep the data per index at a
manageable size (for example for the JVM / memory requirements) per server. And
at the same time minimize the number of indexes involved in each query, to
avoid the associated overhead. In addition it's easy to drop out or replace old
data, as its just disabling an index, but there's no need to rewrite/update any
data. All but the current index can also be highly compacted / optimized
(down to one lucene segment), as they'll never change and backup tools likely
appreciate a large amount of static data as well.

Note that you don't need to manually specify the indexes yourself, but
Elastic Search allows you to read from `_all` or `time_*` indexes at once.
We hide the index details in our REST API, so the client side only has to care
about the REST endpoint like `GET /v1/time`.

elasticsearch.yml
:::::::::::::::::

We don't need to have any custom elasticsearch.yml settings, as we are managing
all of these settings via index templates and cluster API calls.

Articles / videos
:::::::::::::::::

* http://blog.bugsense.com/post/35580279634/indexing-bigdata-with-elasticsearch
* http://edgeofsanity.net/article/2012/12/26/elasticsearch-for-logging.html
* https://github.com/logstash/logstash/wiki/Elasticsearch-Storage-Optimization
* Shay Banon at BB 2012 - http://vimeo.com/44716955
* Shay Banon at BB 2011 - http://vimeo.com/26710663
