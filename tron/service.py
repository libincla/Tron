from twisted.internet import reactor

from tron import job
from tron import action
from tron import command_context
from tron.utils import state


class ServiceInstance(object):
    STATE_DOWN = state.NamedEventState("down")
    STATE_UP = state.NamedEventState("up")
    STATE_KILLING = state.NamedEventState("killing", mark_down=STATE_DOWN)
    STATE_MONITORING = state.NamedEventState("monitoring", mark_down=STATE_DOWN, stop=STATE_KILLING, mark_up=STATE_UP)
    STATE_STARTING = state.NamedEventState("starting", mark_up=STATE_UP, mark_down=STATE_DOWN)

    STATE_UNKNOWN = state.NamedEventState("unknown", mark_monitor=STATE_MONITORING)
    STATE_MONITORING['monitor_fail'] = STATE_UNKNOWN

    STATE_UP['stop'] = STATE_KILLING
    STATE_UP['monitor'] = STATE_MONITORING
    STATE_DOWN['start'] = STATE_STARTING
    
    def __init__(self, service, node, instance_number):
        self.service = service
        self.instance_number = instance_number
        self.node = node

        self.id = "%s.%s" % (service.name, self.instance_number)
        
        self.machine = state.StateMachine(ServiceInstance.STATE_DOWN)
        
        self.pid_path = None
        self.monitor_interval = None

        self.context = command_context.CommandContext(self, service.context)
        
        self.monitor_action = None
        self.start_action = None
        self.kill_action = None
 
    @property
    def state(self):
        return self.machine.state

    @property
    def listen(self):
        return self.machine.listen
        
    def _queue_monitor(self):
        self.monitor_action = None
        reactor.callLater(self._run_monitor, self.monitor_interval)

    def _run_monitor(self):
        if self.monitor_action:
            log.warning("Monitor action already exists, old callLater ?")
            return
        
        self.machine.transition("monitor")
        monitor_command = "cat %(pid_url)s | xargs kill -0" % self.context

        self.monitor_action = action.ActionComand("%s.monitor" % self.id, monitor_command)
        self.monitor_action.machine.listen(action.ActionCommand.COMPLETE, self._monitor_complete_callback)
        self.monitor_action.machine.listen(action.ActionCommand.FAILSTART, self._monitor_complete_failstart)

        self.node.run(self.monitor_action)
        # TODO: Need a timer on this in case the monitor hangs

    def _monitor_complete_callback(self):
        """Callback when our monitor has completed"""
        assert self.monitor_action
        self.last_check = timeutils.current_time()
        
        if self.monitor_action.exit_status != 0:
            self.machine.transition("mark_down")
        else:
            self.machine.transition("mark_up")
            self._queue_monitor()

    def _monitor_complete_failstart(self):
        """Callback when our monitor failed to even start"""
        self.machine.transition("monitor_fail")
        self._queue_monitor()

    def start(self):
        self.machine.transition("start")

    def stop(self):
        self.machine.transition("stop")

    @property
    def data(self):
        # We're going to need to keep track of stuff like pid_file
        raise NotImplementedError()

    def restore(self, data):
        raise NotImplementedError()


class Service(object):
    STATE_DOWN = state.NamedEventState("down")
    STATE_UP = state.NamedEventState("up")
    
    STATE_STOPPING = state.NamedEventState("stopping", mark_all_down=STATE_DOWN)
    STATE_DEGRADED = state.NamedEventState("degraded", stop=STATE_STOPPING, mark_all_up=STATE_UP)
    STATE_STARTING = state.NamedEventState("starting", mark_all_up=STATE_UP)
    
    STATE_DOWN['start'] = STATE_STARTING
    STATE_UP['stop'] = STATE_STOPPING
    STATE_UP['mark_down'] = STATE_DEGRADED
    
    
    def __init__(self, name, command, node_pool=None, context=None):
        self.name = name

        self.scheduler = None
        self.node_pool = node_pool
        self.count = 0
        self.machine = state.StateMachine(Service.STATE_DOWN)

        # Last instance number used
        self._last_instance = None

        self.context = command_context.CommandContext(self, context)
        self.instances = []

    @property
    def state(self):
        return self.machine.state

    @property
    def listen(self):
        return self.machine.listen

    def start(self):
        if self.instances:
            raise Error("Service %s already has instances: %r" % self.instances)
        
        self.machine.transition("start")
        for _ in range(self.count):
            instance = self.build_instance()
            instance.start()

    def stop(self):
        self.machine.transition("stop")

        while self.instances:
            instance = self.instances.pop()
            instance.stop()

    def build_instance(self):
        node = self.node_pool.next()

        if self._last_instance is None:
            self._last_instance = 0
        else:
            self._last_instance += 1

        instance_number = self._last_instance

        service_instance = ServiceInstance(self, node, instance_number)
        self.instances.append(service_instance)

        service_instance.listen(ServiceInstance.STATE_UP, self._instance_up)
        service_instance.listen(ServiceInstance.STATE_DOWN, self._instance_down)

        return service_instance
    
    def _instance_up(self):
        """Callback for service instance to inform us it is up"""
        if all([instance.state == StateInstance.STATE_UP for instance in self.instances]):
            self.machine.transition("mark_all_up")
        
    def _instance_down(self):
        """Callback for service instance to inform us it is down"""
        if all([instance.state == StateInstance.STATE_DOWN for instance in self.instances]):
            self.machine.transition("mark_all_down")
        else:
            self.machine.transition("mark_down")

    def absorb_previous(self, prev_service):
        # Some changes we need to worry about:
        # * Changing instance counts
        # * Changing the command
        # * Changing the node pool
        # * Changes to the context ?
        # * Restart counts for downed services ?

        # First just copy pieces of state that really matter
        self.machine = prev_service.machine
        self._last_instance = prev_service._last_instance
                
        rebuild_all_instances = any([
                                     self.node_pool != prev_service.node_pool, 
                                     self.command != prev_service.command,
                                     self.scheduler != prev_service.scheduler
                                    ])

        if rebuild_all_instances:
            self.start()
            prev_service.stop()
        else:
            # Copy over all the old instances
            self.instances += prev_service.instances
            
            # Now make adjustments to how many there are
            if self.count > prev_service.count:
                # We need to add some instances
                for _ in range(self.count - prev_service.count):
                    self.build_instance()
            elif self.count < prev_service_count:
                for _ in range(prev_service.count - self.count):
                    old_instance = self.instances.pop()
                    # This will fire off an action, we could do something with the result rather than just forget it ever existed.
                    old_instance.stop()
        
        
    @property
    def data(self):
        raise NotImplementedError()
    
    def restore(self, data):
        raise NotImplementedError()