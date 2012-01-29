from types import FunctionType
import functools
import itertools
import logging

from testify import assert_not_reached, turtle
from testify.test_case import TwistedFailureError
from twisted.internet import reactor, defer
from twisted.python import failure

log = logging.getLogger(__name__)

_waiting = False


def wait_for_deferred(deferred, timeout=None):
    """Wait for the deferred object to complete

    Loosly based on twisted trial test case base, allows us to run reactors in
    a test case.
    """

    global _waiting
    if _waiting:
        raise RuntimeError("_wait is not reentrant")
    _waiting = True
    _timed_out = []

    results = []
    failures = []

    def append(any):
        if results is not None:
            results.append(any)

    def timedout_crash():
        _timed_out.append(True)
        reactor.crash()

    def stop_after_defer(ign):
        reactor.stop()

    def stop():
        # Depending on context, sometimes you need to call stop() rather than
        # crash.  I think there is some twisted bug where threads left open
        # don't allow the process to exit

        #reactor.stop()
        reactor.crash()

    def on_failure(f):
        failures.append(f)

    deferred.addErrback(on_failure)

    if timeout is not None:
        reactor.callLater(timeout, stop)

    try:
        deferred.addBoth(append)
        if results:
            # d might have already been fired, in which case append is
            # called synchronously. Avoid any reactor stuff.
            return

        deferred.addBoth(stop_after_defer)
        reactor.stop = stop

        try:
            reactor.run()
        finally:
            del reactor.stop

        if results or _timed_out:
            return

        raise KeyboardInterrupt()

    finally:
        _waiting = False
        results = None
        if failures:
            first_failure = failures[0]

            # By this point we've already lost too much of our exception
            # information (traceback, stack) to re-create the real exception.
            # So what we'll have to do is hope our test framework can handle
            # twisted failure objects so we can get some useful information out
            # of them.
            raise TwistedFailureError(first_failure)
            #raise failure.type, failure.value, failure.getTracebackObject()
            #failures[0].raiseException()


DEFAULT_TIMEOUT = 10.0


def run_reactor(timeout=DEFAULT_TIMEOUT, assert_raises=None):
    """Decorator generator for the fixture decorators

    Args -
        timeout -       (optional) number of seconds to wait for defer to
                        finish up
        assert_raises - (optional) exception that should be generated by
                        deferred
    """

    def wrapper(method):
        def on_timeout(d):
            e = defer.TimeoutError("(%s) still running at %s secs" %
                                   (method.__name__, timeout))
            f = failure.Failure(e)

            try:
                d.errback(f)
            except defer.AlreadyCalledError:
                # if the deferred has been called already but the *back chain
                # is still unfinished, crash the reactor and report timeout
                # error ourself.
                reactor.crash()
                raise

        @functools.wraps(method)
        def run_defer(*args, **kwargs):
            deferred = defer.maybeDeferred(method, *args, **kwargs)

            call = reactor.callLater(timeout, on_timeout, deferred)
            deferred.addBoth(lambda x: call.active() and call.cancel() or x)

            found_error = False
            try:
                wait_for_deferred(deferred)
            except TwistedFailureError, e:
                if assert_raises:
                    d_fail = e.args[0]
                    if issubclass(d_fail.type, assert_raises):
                        found_error = True
                else:
                    raise

            if assert_raises and not found_error:
                assert_not_reached("No exception was raised (expected %s)" %
                                   assert_raises)

            return None

        if isinstance(method, FunctionType):
            run_defer.func_doc = method.func_doc
            run_defer.func_name = method.func_name
        return run_defer
    return wrapper


# A simple test pool that automatically starts any command
class TestNode(turtle.Turtle):

    def __init__(self, hostname=None):
        self.name = hostname

    def run(self, runnable):
        runnable.started()
        return turtle.Turtle()


class TestPool(object):
    _node = None

    def __init__(self, *node_names):
        self.nodes = []
        self._ndx_cycle = None
        for hostname in node_names:
            self.nodes.append(TestNode(hostname=hostname))

        if self.nodes:
            self._ndx_cycle = itertools.cycle(range(0, len(self.nodes)))

    def __getitem__(self, value):
        for node in self.nodes:
            if node.hostname == value:
                return node
        else:
            raise KeyError

    def next(self):
        if not self.nodes:
            self.nodes.append(TestNode())

        if self._ndx_cycle:
            return self.nodes[self._ndx_cycle.next()]
        else:
            return self.nodes[0]

    next_round_robin = next
