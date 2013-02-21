import os
import argparse
from ConfigParser import ConfigParser, NoOptionError
import sys
from datetime import datetime

import gevent
from gevent.queue import JoinableQueue
from gevent.pool import Group

from aggregator import __version__, logger
from aggregator.util import (configure_logger, LOG_LEVELS,
                             word2daterange)
from aggregator.history import History
from aggregator.sequence import Sequence


class AlreadyDoneError(Exception):
    pass


def _mkdate(datestring):
    return datetime.strptime(datestring, '%Y-%m-%d').date()


def _get_data(queue, callable, start_date, end_date):
    #logger.info('Getting from %s' % callable)
    try:
        for item in callable(start_date, end_date):
            queue.put(item)
    finally:
        queue.put('END')


def _put_data(callable, data):
    #logger.info('Pushing to %s' % callable)
    return callable(data)


def _push_to_target(queue, targets, batch_size):
    """Get a batch of elements from the queue, and push it to the targets.

    This function returns True if it proceeded all the elements in the queue,
    and there isn't anything more to read.
    """
    if queue.empty():
        return False    # nothing

    batch = []
    eoq = False

    # collecting a batch
    while len(batch) < batch_size:
        try:
            item = queue.get()
            if item == 'END':
                # reached the end
                eoq = True
                break
            batch.append(item)
        finally:
            queue.task_done()

    if len(batch) != 0:
        #logger.info('Pushing %s items', len(batch))
        greenlets = Group()
        for plugin in targets:
            greenlets.spawn(_put_data, plugin, batch)
        greenlets.join()

    return eoq


def extract(config, start_date, end_date, sequence=None, batch_size=None,
            force=False):
    """Reads the configuration file and does the job.
    """
    parser = ConfigParser(defaults={'here': os.path.dirname(config)})
    parser.read(config)

    try:
        batch_size = parser.get('monolith', 'batch_size')
    except NoOptionError:
        # using the default value
        if batch_size is None:
            batch_size = 100

    logger.info('size of the batches: %s', batch_size)

    # creating the sequence
    sequence = Sequence(parser, sequence)

    # load the history
    try:
        history_db = parser.get('monolith', 'history')
    except NoOptionError:
        raise ValueError("You need a history db option")

    history = History(sqluri=history_db)
    # run the sequence by phase
    queue = JoinableQueue()

    for phase, sources, targets in sequence:
        for source in sources:
            if history.exists(source, start_date, end_date) and not force:
                raise AlreadyDoneError()

        logger.info('Running phase %r' % phase)
        greenlets = Group()

        # each callable will push its result in the queue
        for source in sources:
            greenlets.spawn(_get_data, queue, source, start_date, end_date)

        # looking at the queue
        processed = 0
        while processed < len(sources):
            eoq = _push_to_target(queue, targets, batch_size)
            if eoq:
                processed += 1
            gevent.sleep(0)

        greenlets.join()

        # if we reach this point we can consider the transaction a success
        # for these sources
        history.add_entry(sources, start_date, end_date)


_DATES = ['today', 'yesterday', 'last-week', 'last-month',
          'last-year']


def main():
    parser = argparse.ArgumentParser(description='Monolith Aggregator')

    parser.add_argument('--version', action='store_true', default=False,
                        help='Displays version and exits.')

    date_group = parser.add_mutually_exclusive_group()
    date_group.add_argument('--date', default=None, choices=_DATES,
                            help='Date')
    date_group.add_argument('--start-date', default=None, type=_mkdate,
                            help='Start date.')
    parser.add_argument('--end-date', default=None, type=_mkdate,
                        help='End date.')
    parser.add_argument('config', help='Configuration file.',)
    parser.add_argument('--log-level', dest='loglevel', default='info',
                        choices=LOG_LEVELS.keys() + [key.upper() for key in
                                                     LOG_LEVELS.keys()],
                        help="log level")
    parser.add_argument('--log-output', dest='logoutput', default='-',
                        help="log output")
    parser.add_argument('--sequence', dest='sequence', default=None,
                        help='A comma-separated list of sequences.')
    parser.add_argument('--batch-size', dest='batch_size', default=None,
                        type=int,
                        help='The size of the batch when writing')
    parser.add_argument('--force', action='store_true', default=False,
                        help='Forces a run')

    args = parser.parse_args()

    if args.version:
        print(__version__)
        sys.exit(0)

    if args.date is not None:
        start, end = word2daterange(args.date)
    else:
        start, end = args.start_date, args.end_date

    configure_logger(logger, args.loglevel, args.logoutput)
    extract(args.config, start, end, args.sequence, args.batch_size,
            args.force)


if __name__ == '__main__':
    main()
