# Copyright (c) 2015 Akanda, Inc. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License"); you may
# not use this file except in compliance with the License. You may obtain
# a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
# WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
# License for the specific language governing permissions and limitations
# under the License.

from oslo_config import cfg
from oslo_log import log as logging

from akanda.rug.drivers.router import Router

DRIVER_OPTS = [
    cfg.ListOpt('enabled_drivers',
                default=['router', ],
                help='list of drivers the rug process will load'),
]

cfg.CONF.register_opts(DRIVER_OPTS)

LOG = logging.getLogger(__name__)

AVAILABLE_DRIVERS = {'router': Router}


class InvalidDriverException(Exception):
    """Triggered when driver is not available in AVAILABLE_DRIVERS"""
    pass


def get(requested_driver):
    """Returns driver class based on the requested_driver param
    will raise InvalidDriverException if not listed in the config option
    cfg.CONF.available_drivers.

    :param requested_driver: name of desired driver
    :return: returns driver object
    """
    if requested_driver in AVAILABLE_DRIVERS:
        return AVAILABLE_DRIVERS[requested_driver]

    raise InvalidDriverException(
        'Failed loading driver: %s' % requested_driver
    )


def enabled_drivers():
    for driver in cfg.CONF.enabled_drivers:
        try:
            d = get(driver)
            yield d
        except InvalidDriverException as e:
            LOG.exception(e)
            pass
