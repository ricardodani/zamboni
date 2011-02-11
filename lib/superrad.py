import functools
import time

from django.test import testcases

import test_utils
from nose.plugins import Plugin


def timed(msg):
    def dec(f):
        @functools.wraps(f)
        def wrapper(*args, **kw):
            start = time.time()
            print '%s...' % msg
            f(*args, **kw)
            print '%.2fs' % (time.time() - start)
        return wrapper
    return dec


class SuperRad(Plugin):
    """get superrad"""
    name = 'superrad'
    enabled = True
    score = 501

    def __init__(self, *args, **kw):
        super(SuperRad, self).__init__(*args, **kw)
        self.fixtures = set()

    def wantClass(self, cls):
        want = issubclass(cls, TestCase)
        if want:
            # print 'want:', cls
            self.fixtures.update(getattr(cls, 'fixtures', []))
        return want

    @timed('Loading fixtures')
    def prepareTestRunner(self, runner):
        print '\n'.join(sorted(self.fixtures))
        testcases.call_command('loaddata', *self.fixtures, verbosity=0,
                               commit=False, database=testcases.DEFAULT_DB_ALIAS)

    @timed('Cleaning up')
    def finalize(self, result):
        testcases.call_command('flush', verbosity=0, interactive=False,
                               database=testcases.DEFAULT_DB_ALIAS)


class TestCase(test_utils.TestCase):

    def _fixture_setup(self):
        # return super(TestCase, self)._fixture_setup()

        # if not connections_support_transactions():
        #     return super(TestCase, self)._fixture_setup()

        # If the test case has a multi_db=True flag, setup all databases.
        # Otherwise, just use default.
        if getattr(self, 'multi_db', False):
            databases = testcases.connections
        else:
            databases = [testcases.DEFAULT_DB_ALIAS]

        for db in databases:
            testcases.transaction.enter_transaction_management(using=db)
            testcases.transaction.managed(True, using=db)
        testcases.disable_transaction_methods()

        from django.contrib.sites.models import Site
        Site.objects.clear_cache()

        # for db in databases:
        #     if hasattr(self, 'fixtures'):
        #         call_command('loaddata', *self.fixtures, **{
        #                                                     'verbosity': 0,
        #                                                     'commit': False,
        #                                                     'database': db
        #                                                    })

    def _fixture_teardown(self):
        # return super(TestCase, self)._fixture_teardown()
        if not testcases.connections_support_transactions():
            return super(TestCase, self)._fixture_teardown()

        # If the test case has a multi_db=True flag, teardown all databases.
        # Otherwise, just teardown default.
        if getattr(self, 'multi_db', False):
            databases = testcases.connections
        else:
            databases = [testcases.DEFAULT_DB_ALIAS]

        testcases.restore_transaction_methods()
        for db in databases:
            testcases.transaction.rollback(using=db)
            testcases.transaction.leave_transaction_management(using=db)
