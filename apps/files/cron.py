import os
import shutil
import stat
import time

from django.conf import settings

import cronjobs
import commonware.log
from celery.messaging import establish_connection

import amo.utils
from . import tasks
from .models import File


log = commonware.log.getLogger('z.cron')


@cronjobs.register
def cleanup_extracted_file():
    log.info('Removing extracted files for file viewer.')
    root = os.path.join(settings.TMP_PATH, 'file_viewer')
    for path in os.listdir(root):
        path = os.path.join(root, path)
        age = time.time() - os.stat(path)[stat.ST_CTIME]
        if (age) > (60 * 60):
            log.info('Removing extracted files: %s, %dsecs old.' % (path, age))
            shutil.rmtree(path)
