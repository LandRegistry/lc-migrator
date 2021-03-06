import config
import importlib
import os
from application.routes import migrate
from log.logger import setup_logging
import sys

exit(1)

if len(sys.argv) < 3:
    print('Insuffcient parameters specified')
    exit()

s = sys.argv[1]
e = sys.argv[2]


cfg = 'Config'
c = getattr(importlib.import_module('config'), cfg)
config = {}

for key in dir(c):
    if key.isupper():
        config[key] = getattr(c, key)

setup_logging(config)
migrate(config, s, e)


