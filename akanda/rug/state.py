# Copyright 2014 DreamHost, LLC
#
# Author: DreamHost, LLC
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


"""State machine for managing a router.

"""

# See state machine diagram and description:
# https://docs.google.com/a/dreamhost.com/document/d/1Ed5wDqCHW-CUt67ufjOUq4uYj0ECS5PweHxoueUoYUI/edit # noqa

import collections
import itertools
import logging

from akanda.rug.event import POLL, CREATE, READ, UPDATE, DELETE, REBUILD
from akanda.rug import vm_manager


class StateParams(object):
    def __init__(self, vm, log, queue, bandwidth_callback,
                 reboot_error_threshold):
        self.vm = vm
        self.log = log
        self.queue = queue
        self.bandwidth_callback = bandwidth_callback
        self.reboot_error_threshold = reboot_error_threshold


class State(object):

    def __init__(self, params):
        self.params = params

    @property
    def log(self):
        return self.params.log

    @property
    def queue(self):
        return self.params.queue

    @property
    def vm(self):
        return self.params.vm

    @property
    def name(self):
        return self.__class__.__name__

    def __str__(self):
        return self.name

    def execute(self, action, worker_context):
        return action

    def transition(self, action, worker_context):
        return self


class CalcAction(State):
    def execute(self, action, worker_context):
        queue = self.queue
        if DELETE in queue:
            self.log.debug('shortcutting to delete')
            return DELETE

        while queue:
            self.log.debug(
                'action = %s, len(queue) = %s, queue = %s',
                action,
                len(queue),
                list(itertools.islice(queue, 0, 60))
            )

            if action == UPDATE and queue[0] == CREATE:
                # upgrade to CREATE from UPDATE by taking the next
                # item from the queue
                self.log.debug('upgrading from update to create')
                action = queue.popleft()
                continue

            elif action == UPDATE and queue[0] == REBUILD:
                # upgrade to REBUILD from UPDATE by taking the next
                # item from the queue
                self.log.debug('upgrading from update to rebuild')
                action = queue.popleft()
                continue

            elif action == CREATE and queue[0] == UPDATE:
                # CREATE implies an UPDATE so eat the update event
                # without changing the action
                self.log.debug('merging create and update')
                queue.popleft()
                continue

            elif action and queue[0] == POLL:
                # Throw away a poll following any other valid action,
                # because a create or update will automatically handle
                # the poll and repeated polls are not needed.
                self.log.debug('discarding poll event following action %s',
                               action)
                queue.popleft()
                continue

            elif action and action != POLL and action != queue[0]:
                # We are not polling and the next action is something
                # different from what we are doing, so just do the
                # current action.
                self.log.debug('done collapsing events')
                break

            self.log.debug('popping action from queue')
            action = queue.popleft()

        return action

    def transition(self, action, worker_context):
        if self.vm.state == vm_manager.GONE:
            return StopVM(self.params)
        elif action == DELETE:
            return StopVM(self.params)
        elif action == REBUILD:
            return RebuildVM(self.params)
        elif self.vm.state == vm_manager.BOOTING:
            return CheckBoot(self.params)
        elif self.vm.state == vm_manager.DOWN:
            return CreateVM(self.params)
        else:
            return Alive(self.params)


class PushUpdate(State):
    """Put an update instruction on the queue for the state machine.
    """
    def execute(self, action, worker_context):
        # Put the action back on the front of the queue.
        self.queue.appendleft(UPDATE)

    def transition(self, action, worker_context):
        return CalcAction(self.params)


class Alive(State):
    def execute(self, action, worker_context):
        self.vm.update_state(worker_context)
        return action

    def transition(self, action, worker_context):
        if self.vm.state == vm_manager.GONE:
            return StopVM(self.params)
        elif self.vm.state == vm_manager.DOWN:
            return CreateVM(self.params)
        elif action == POLL and self.vm.state == vm_manager.CONFIGURED:
            return CalcAction(self.params)
        elif action == READ and self.vm.state == vm_manager.CONFIGURED:
            return ReadStats(self.params)
        else:
            return ConfigureVM(self.params)


class CreateVM(State):
    def execute(self, action, worker_context):
        # Check for a boot loop.
        # FIXME(dhellmann): This does not handle the case where we are
        # trying to force a reboot after fixing the problem that
        # caused the loop in the first place.
        if self.vm.attempts >= self.params.reboot_error_threshold:
            self.log.info('dropping out of boot loop after %s trials',
                          self.vm.attempts)
            self.vm.set_error(worker_context)
            return action
        self.vm.boot(worker_context)
        self.log.debug('CreateVM attempt %s/%s',
                       self.vm.attempts,
                       self.params.reboot_error_threshold)
        return action

    def transition(self, action, worker_context):
        if self.vm.state == vm_manager.GONE:
            return StopVM(self.params)
        elif self.vm.state == vm_manager.ERROR:
            return CalcAction(self.params)
        return CheckBoot(self.params)


class CheckBoot(State):
    def execute(self, action, worker_context):
        self.vm.check_boot(worker_context)
        # Put the action back on the front of the queue so that we can yield
        # and handle it in another state machine traversal (which will proceed
        # from CalcAction directly to CheckBoot).
        if self.vm.state != vm_manager.GONE:
            self.queue.appendleft(action)
        return action

    def transition(self, action, worker_context):
        if self.vm.state == vm_manager.GONE:
            return StopVM(self.params)
        if self.vm.state == vm_manager.UP:
            return ConfigureVM(self.params)
        return CalcAction(self.params)


class StopVM(State):
    def execute(self, action, worker_context):
        self.vm.stop(worker_context)
        if self.vm.state == vm_manager.GONE:
            # Force the action to delete since the router isn't there
            # any more.
            return DELETE
        return action

    def transition(self, action, worker_context):
        if self.vm.state not in (vm_manager.DOWN, vm_manager.GONE):
            return self
        if self.vm.state == vm_manager.GONE:
            return Exit(self.params)
        if action == DELETE:
            return Exit(self.params)
        return CreateVM(self.params)


class RebuildVM(State):
    def execute(self, action, worker_context):
        # If we are being told explicitly to rebuild the VM, we should
        # ignore any error status and try to do the rebuild.
        if self.vm.state == vm_manager.ERROR:
            self.vm.clear_error(worker_context)
        self.vm.stop(worker_context)
        if self.vm.state == vm_manager.GONE:
            # Force the action to delete since the router isn't there
            # any more.
            return DELETE
        # Re-create the VM
        return CREATE

    def transition(self, action, worker_context):
        if self.vm.state not in (vm_manager.DOWN, vm_manager.GONE):
            return self
        if self.vm.state == vm_manager.GONE:
            return Exit(self.params)
        return CreateVM(self.params)


class Exit(State):
    pass


class ConfigureVM(State):
    def execute(self, action, worker_context):
        self.vm.configure(worker_context)
        if self.vm.state == vm_manager.CONFIGURED:
            if action == READ:
                return READ
            else:
                return POLL
        else:
            return action

    def transition(self, action, worker_context):
        if self.vm.state in (vm_manager.RESTART,
                             vm_manager.DOWN,
                             vm_manager.GONE):
            return StopVM(self.params)
        if self.vm.state == vm_manager.UP:
            return PushUpdate(self.params)
        # Below here, assume vm.state == vm_manager.CONFIGURED
        if action == READ:
            return ReadStats(self.params)
        return CalcAction(self.params)


class ReadStats(State):
    def execute(self, action, worker_context):
        stats = self.vm.read_stats()
        self.params.bandwidth_callback(stats)
        return POLL

    def transition(self, action, worker_context):
        return CalcAction(self.params)


class Automaton(object):
    def __init__(self, router_id, tenant_id,
                 delete_callback, bandwidth_callback,
                 worker_context, queue_warning_threshold,
                 reboot_error_threshold):
        """
        :param router_id: UUID of the router being managed
        :type router_id: str
        :param tenant_id: UUID of the tenant being managed
        :type tenant_id: str
        :param delete_callback: Invoked when the Automaton decides
                                the router should be deleted.
        :type delete_callback: callable
        :param bandwidth_callback: To be invoked when the Automaton
                                   needs to report how much bandwidth
                                   a router has used.
        :type bandwidth_callback: callable taking router_id and bandwidth
                                  info dict
        :param worker_context: a WorkerContext
        :type worker_context: WorkerContext
        :param queue_warning_threshold: Limit after which adding items
                                        to the queue triggers a warning.
        :type queue_warning_threshold: int
        :param reboot_error_threshold: Limit after which trying to reboot
                                       the router puts it into an error state.
        :type reboot_error_threshold: int
        """
        self.router_id = router_id
        self.tenant_id = tenant_id
        self._delete_callback = delete_callback
        self._queue_warning_threshold = queue_warning_threshold
        self._reboot_error_threshold = reboot_error_threshold
        self.deleted = False
        self.bandwidth_callback = bandwidth_callback
        self._queue = collections.deque()
        self.log = logging.getLogger(__name__ + '.' + router_id)

        self.action = POLL
        self.vm = vm_manager.VmManager(router_id, tenant_id, self.log,
                                       worker_context)
        self._state_params = StateParams(
            self.vm,
            self.log,
            self._queue,
            self.bandwidth_callback,
            self._reboot_error_threshold,
        )
        self.state = CalcAction(self._state_params)

    def service_shutdown(self):
        "Called when the parent process is being stopped"

    def _do_delete(self):
        if self._delete_callback is not None:
            self.log.debug('calling delete callback')
            self._delete_callback()
            # Avoid calling the delete callback more than once.
            self._delete_callback = None
        # Remember that this router has been deleted
        self.deleted = True

    def update(self, worker_context):
        "Called when the router config should be changed"
        while self._queue:
            while True:
                if self.deleted:
                    self.log.debug(
                        'skipping update because the router is being deleted'
                    )
                    return

                try:
                    self.log.debug('%s.execute(%s) vm.state=%s',
                                   self.state, self.action, self.vm.state)
                    self.action = self.state.execute(
                        self.action,
                        worker_context,
                    )
                    self.log.debug('%s.execute -> %s vm.state=%s',
                                   self.state, self.action, self.vm.state)
                except:
                    self.log.exception(
                        '%s.execute() failed for action: %s',
                        self.state,
                        self.action
                    )

                old_state = self.state
                self.state = self.state.transition(
                    self.action,
                    worker_context,
                )
                self.log.debug('%s.transition(%s) -> %s vm.state=%s',
                               old_state, self.action, self.state,
                               self.vm.state)

                # Yield control each time we stop to figure out what
                # to do next.
                if isinstance(self.state, CalcAction):
                    return  # yield

                # We have reached the exit state, so the router has
                # been deleted somehow.
                if isinstance(self.state, Exit):
                    self._do_delete()
                    return

    def send_message(self, message):
        "Called when the worker put a message in the state machine queue"
        if self.deleted:
            # Ignore any more incoming messages
            self.log.debug(
                'deleted state machine, ignoring incoming message %s',
                message)
            return False

        if message.crud == POLL and self.vm.state == vm_manager.ERROR:
            self.log.info(
                'Router status is ERROR, ignoring POLL message: %s',
                message,
            )
            return False

        self._queue.append(message.crud)
        queue_len = len(self._queue)
        if queue_len > self._queue_warning_threshold:
            logger = self.log.warning
        else:
            logger = self.log.debug
        logger('incoming message brings queue length to %s', queue_len)
        return True

    def has_more_work(self):
        "Called to check if there are more messages in the state machine queue"
        return (not self.deleted) and bool(self._queue)
