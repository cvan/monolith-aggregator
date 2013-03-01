from functools import partial

import gevent
from gevent.queue import Queue
from gevent.pool import Group

from aggregator import logger


class AlreadyDoneError(Exception):
    pass


class InjectError(Exception):
    pass


class ExtractError(Exception):
    pass


class RunError(Exception):
    def __init__(self, errors):
        self.errors = errors

    def __str__(self):
        msg = '%d failures\n\n' % len(self.errors)

        for index, (error, plugin, greenlet) in enumerate(self.errors):
            msg += '%d. %s in %s. error: %s' % (index + 1, error, plugin,
                                                greenlet.exception)
        return msg


class Engine(object):

    def __init__(self, sequence, history, phase_hook=None, batch_size=100,
                 force=False, retries=3):
        self.sequence = sequence
        self.history = history
        self.queue = Queue()
        self.phase_hook = phase_hook
        self.batch_size = batch_size
        self.force = force
        self.retries = retries
        self.errors = []

    def _push_to_target(self, targets):
        """Get a batch of elements from the queue, and push it to the targets.

        This function returns True if it proceeded all the elements in
        the queue, and there isn't anything more to read.
        """
        if self.queue.empty():
            return 0    # nothing

        batch = []
        pushed = 0

        # collecting a batch
        while len(batch) < self.batch_size:
            item = self.queue.get()
            if item == 'END':
                pushed += 1  # the 'END' item
                break
            batch.append(item)

        if len(batch) != 0:
            greenlets = Group()
            for plugin in targets:
                green = greenlets.spawn(self._put_data, plugin, batch)
                green.link_exception(partial(self._error, InjectError, plugin))
            greenlets.join()
            pushed += len(batch)

        return pushed

    #
    # transaction managment
    #
    def _start_transactions(self, plugins):
        for plugin in plugins:
            plugin.start_transaction()

    def _commit_transactions(self, plugins):
        # XXX what happends when this fails?
        for plugin in plugins:
            plugin.commit_transaction()

    def _rollback_transactions(self, plugins):
        for plugin in plugins:
            plugin.rollback_transaction()

    def _put_data(self, plugin, data):
        return plugin.inject(data, overwrite=self.force)

    def _get_data(self, plugin, start_date, end_date):
        try:
            for item in plugin.extract(start_date, end_date):
                self.queue.put((plugin.get_id(), item))
        finally:
            self.queue.put('END')

    def _error(self, exception, plugin, greenlet):
        self.errors.append((exception, plugin, greenlet))

    def _run_phase(self, phase, start_date, end_date):
        phase, sources, targets = phase
        logger.info('Running phase %r' % phase)
        self._reset_counters()

        for source in sources:
            exists = self.history.exists(source, start_date, end_date)
            if exists and not self.force:
                raise AlreadyDoneError(source.get_id(), start_date, end_date)

        self._start_transactions(targets)
        self.history.start_transaction()
        try:
            greenlets = Group()
            # each callable will push its result in the queue
            for source in sources:
                green = greenlets.spawn(self._get_data, source,
                                        start_date, end_date)
                green.link_exception(partial(self._error, ExtractError,
                                             source))

            # looking at the queue
            pushed = 0

            while len(greenlets) > 0 or self.queue.qsize() > 0:
                gevent.sleep(0)
                pushed += self._push_to_target(targets)
                # let's see if we have some errors
                if len(self.errors) > 0:
                    # yeah! we need to rollback
                    # XXX later we'll do a source-by-source rollback
                    raise RunError(self.errors)

            self.history.add_entry(sources, start_date, end_date, pushed)
        except Exception:
            self._rollback_transactions(targets)
            self.history.rollback_transaction()
            raise
        else:
            self._commit_transactions(targets)
            self.history.commit_transaction()

    def _purge(self, start_date, end_date):
        for phase, sources, targets in self.sequence:
            for source in sources:
                try:
                    source.purge(start_date, end_date)
                except Exception:
                    logger.error('Failed to purge %r' % source.get_id())

    def _retry(self, func, *args, **kw):
        tries = 0
        retries = self.retries
        while tries < retries:
            try:
                return func(*args, **kw)
            except Exception, exc:
                self.queue.queue.clear()
                if isinstance(exc, AlreadyDoneError):
                    raise
                logger.exception('%s failed (%d/%d)' % (func, tries + 1,
                                                        retries))
                tries += 1
        raise

    def _reset_counters(self):
        self.errors = []

    def run(self, start_date, end_date, purge_only=False):
        self._reset_counters()

        if not purge_only:
            for phase in self.sequence:
                if self.queue.qsize() > 0:
                    raise ValueError('The queue still has %d elements' %
                                     self.queue.qsize())

                self._retry(self._run_phase, phase, start_date, end_date)

        # purging
        self._retry(self._purge, start_date, end_date)
        return 0
