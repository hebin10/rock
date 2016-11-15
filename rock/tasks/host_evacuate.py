# Copyright 2011 OpenStack Foundation.
# All Rights Reserved.
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.

import datetime
import json
import time

from oslo_config import cfg
from oslo_log import log as logging
from rock.tasks import flow_utils

LOG = logging.getLogger(__name__)
CONF = cfg.CONF


class HostEvacuate(flow_utils.BaseTask):
    default_provides = ('message_body', 'host_evacuate_result')

    def execute(self, target, taskflow_uuid, host_power_off_result):
        n_client = flow_utils.get_nova_client()
        servers, servers_id, servers_ori_state = self.get_servers(
            n_client, target)
        if len(servers) == 0:
            LOG.info("There is no instance on host %s, no need to evacuate"
                     % target)
            return [], True
        message_generator = 'message_generator_for_' + CONF.message_report_to

        # Force down nova compute of target
        # self.force_down_nova_compute(n_client, host)

        # Check nova compute state of target
        nova_compute_state = self.check_nova_compute_state(n_client, target)
        if not nova_compute_state or not host_power_off_result:
            if not nova_compute_state:
                LOG.warning("Failed to perform evacuation of compute host: %s "
                            "due to nova compute service is still up" % target)
            if not host_power_off_result:
                LOG.warning("Failed to perform evacuation of compute host: %s "
                            "due to can't state power status of this host"
                            % target)
            evacuate_result = self.get_evacuate_results(
                n_client, servers_id, target, taskflow_uuid,
                message_generator=message_generator)
            return evacuate_result[0], evacuate_result[2]

        # Evacuate servers on the target
        self.evacuate_servers(servers, n_client)

        # Check evacuate status, while all servers successfully evacuated,
        # it will return, otherwise it will wait check_times * time_delta.
        self.check_evacuate_status(
            n_client, servers_id, target,
            check_times=CONF.host_evacuate.check_times,
            time_delta=CONF.host_evacuate.check_interval)

        evacuate_result = self.get_evacuate_results(
            n_client, servers_id, target, taskflow_uuid,
            message_generator=message_generator)

        if not evacuate_result[2]:
            self.reset_state(n_client=n_client, ori_state=servers_ori_state,
                             current_state=evacuate_result[1])

        return evacuate_result[0], evacuate_result[2]

    @staticmethod
    def get_servers(n_client, host):
        try:
            servers = n_client.servers.list(search_opts={
                'host': host,
                'all_tenants': 1
            })
        except Exception as err:
            LOG.warning("Cant't get servers on host %s due to %s"
                        % (host, err.message))
            return [], [], {}
        servers_id = []
        servers_state = {}
        for server in servers:
            servers_id.append(server.id)
            servers_state[server.id] = getattr(
                server, "OS-EXT-STS:vm_state", "active")
        return servers, servers_id, servers_state

    @staticmethod
    def evacuate_servers(servers, n_client):
        on_shared_storage = CONF.host_evacuate.on_shared_storage
        for server in servers:
            try:
                LOG.debug("Request to evacuate server: %s" % server.id)
                res = n_client.servers.evacuate(
                    server=server.id,
                    on_shared_storage=on_shared_storage)
                if res[0].status_code == 200:
                    LOG.info("Request to evacuate server %s accepted"
                             % server.id)
                else:
                    LOG.warning("Request to evacuate server %s failed due "
                                "to %s"
                                % (server.id, res[0].reason))
            except Exception as err:
                LOG.warning("Request to evacuate server %s failed due "
                            "to %s" % (server.id, err.message))

    @staticmethod
    def force_down_nova_compute(n_client, host):
        n_client.services.force_down(host=host, binary='nova-compute')

    @staticmethod
    def check_nova_compute_state(n_client, host, check_times=20, time_delta=5):
        LOG.info("Checking nova compute state of host %s , ensure it is"
                 " in state 'down'." % host)
        for t in range(check_times):
            nova_compute = n_client.services.list(
                host=host, binary='nova-compute')
            state = nova_compute[0].state
            if state == u'up':
                LOG.warning("Nova compute of host %s is up, waiting it"
                            " to down." % host)
                time.sleep(time_delta)
            else:
                LOG.info("Nova compute of host %s is down." % host)
                # If nova compute is down, return True
                return True
        # If nova compute is always up, return False
        return False

    @staticmethod
    def check_evacuate_status(n_client, vms_uuid, vm_origin_host,
                              check_times=6, time_delta=15):
        LOG.info(
            "Checking evacuate status. Check times: %s, check interval: %ss." %
            (check_times, time_delta))
        continue_flag = True
        for i in range(check_times):
            if continue_flag:
                for vm_id in vms_uuid:
                    vm = n_client.servers.get(vm_id)
                    vm_task_state = getattr(vm, 'OS-EXT-STS:task_state', None)
                    vm_host = getattr(vm, 'OS-EXT-SRV-ATTR:host', None)

                    if (vm_task_state is not None) or \
                            (vm_host == unicode(vm_origin_host)):
                        time.sleep(time_delta)
                        continue_flag = True
                        break

                    continue_flag = False
            else:
                break

    def get_evacuate_results(
            self, n_client, vms_uuid, vm_origin_host, taskflow_uuid,
            message_generator='message_generator_for_activemq'):

        results = []
        failed_uuid_and_state = {}
        generator = getattr(self, message_generator, None)
        if generator is None:
            LOG.error("Invalid message generator: %s" % message_generator)
            for vm_id in vms_uuid:
                vm = n_client.servers.get(vm_id)
                vm_task_state = getattr(vm, 'OS-EXT-STS:task_state', None)
                vm_host = getattr(vm, 'OS-EXT-SRV-ATTR:host', None)
                vm_state = getattr(vm, "OS-EXT-STS:vm_state", None)

                if (vm_task_state is None) and \
                        (vm_host != unicode(vm_origin_host)):
                    LOG.info(
                        "Successfully evacuated server: %s, origin_host: %s"
                        ", current_host: %s" %
                        (vm.id, vm_origin_host, vm_host))

                else:
                    LOG.warning(
                        "Failed evacuate server: %s, origin_host: %s, "
                        "current_host: %s, vm_task_state: %s" %
                        (vm.id, vm_origin_host, vm_host, vm_task_state))

                    if vm_host == unicode(vm_origin_host) and \
                            vm_task_state is None:
                        failed_uuid_and_state[vm_id] = vm_state
                        LOG.warning("Mark server %s evacuated failed and "
                                    "should do evacuate again" % vm_id)

            if len(failed_uuid_and_state) > 0:
                return results, failed_uuid_and_state, False
            else:
                return results, failed_uuid_and_state, True

        for vm_id in vms_uuid:
            vm = n_client.servers.get(vm_id)
            vm_task_state = getattr(vm, 'OS-EXT-STS:task_state', None)
            vm_host = getattr(vm, 'OS-EXT-SRV-ATTR:host', None)
            vm_state = getattr(vm, "OS-EXT-STS:vm_state", None)

            if (vm_task_state is None) and \
                    (vm_host != unicode(vm_origin_host)):

                results.append(generator(vm, True,
                                         taskflow_uuid=taskflow_uuid,
                                         origin_host=vm_origin_host))

                LOG.info("Successfully evacuated server: %s, origin_host: %s"
                         ", current_host: %s" %
                         (vm.id, vm_origin_host, vm_host))

            else:
                results.append(generator(vm, False,
                                         taskflow_uuid=taskflow_uuid,
                                         origin_host=vm_origin_host))

                LOG.warning(
                    "Failed evacuate server: %s, origin_host: %s, "
                    "current_host: %s, vm_task_state: %s" %
                    (vm.id, vm_origin_host, vm_host, vm_task_state))

                if vm_host == unicode(vm_origin_host) and \
                        vm_task_state is None:
                    failed_uuid_and_state[vm_id] = vm_state
                    LOG.warning("Mark server %s evacuated failed and "
                                "should do evacuate again" % vm_id)

        if len(failed_uuid_and_state) > 0:
            return results, failed_uuid_and_state, False
        else:
            return results, failed_uuid_and_state, True

    @staticmethod
    def message_generator_for_kiki(vm, success, **kwargs):
        """Generate message of single instance for kiki.

        :param vm: virtual machine instance.
        :param success: indicate evacuation status.
        :param kwargs: some extra arguments for specific generator.
        :return: dict.
        """
        instance_id = getattr(vm, 'id', 'null')
        instance_name = getattr(vm, 'name', 'null')
        project_id = getattr(vm, 'tenant_id', 'null')
        user_id = getattr(vm, 'user_id', 'null')
        instance_status = getattr(vm, 'status', 'null')
        availability_zone = getattr(vm, 'OS-EXT-AZ:availability_zone', 'null')
        created_at = getattr(vm, 'created', 'null')
        networks = getattr(vm, 'networks', 'null')
        power_state = getattr(vm, 'OS-EXT-STS:power_state', 'null')
        task_state = getattr(vm, 'OS-EXT-STS:task_state', 'null')
        origin_host = kwargs.get('origin_host')
        current_host = getattr(vm, 'OS-EXT-SRV-ATTR:host', 'null')
        timestamp = datetime.datetime.utcnow().strftime('%m/%d/%Y %H:%M:%S')

        if success:
            evacuation = 'evacuated successfully.'
        else:
            evacuation = 'failed to evacuate.'

        summery = 'Instance: ' + instance_name + '(' + instance_id + ') ' + \
                  evacuation
        result = {
            'summary': summery,
            'timestamp': timestamp,
            'power_state': power_state,
            'task_state': task_state,
            'instance_status': instance_status,
            'instance_id': instance_id,
            'instance_name': instance_name,
            'user_id': user_id,
            'project_id': project_id,
            'origin_host': origin_host,
            'current_host': current_host,
            'availability_zone': availability_zone,
            'instance_created_at': created_at,
            'networks': networks
        }

        return result

    def message_generator_for_activemq(self, vm, success, **kwargs):
        target = str(getattr(vm, 'OS-EXT-SRV-ATTR:host'))
        taskflow_uuid = kwargs.get('taskflow_uuid', None)
        severity = '2'
        if success:
            summary = 'vm ' + str(vm.id) + '|' + self.get_vm_ip(vm) + ' ' + \
                      target + '|' + self.get_target_ip(target) + ' HA SUCCESS'
        else:
            summary = 'vm ' + str(vm.id) + '|' + self.get_vm_ip(vm) + ' ' + \
                      target + '|' + self.get_target_ip(target) + ' HA FAILED'
        last_occurrence = datetime.datetime.utcnow(). \
            strftime('%m/%d/%Y %H:%M:%S')
        status = 1
        source_id = 10
        # source_identifier =
        source_event_id = taskflow_uuid
        source_ci_name = str(vm.id)
        source_alert_key = 'ROCK_VM_HA'
        source_severity = 'INFO'

        single_result = {
            'Severity': severity,
            'Summary': summary,
            'LastOccurrence': last_occurrence,
            'Status': status,
            'SourceID': source_id,
            'SourceEventID': source_event_id,
            'SourceCIName': source_ci_name,
            'SourceAlertKey': source_alert_key,
            'SourceSeverity': source_severity
        }

        return json.dumps(single_result)

    @staticmethod
    def get_vm_ip(vm):
        vm_ip = ''
        for k, v in vm.networks.items():
            for ip in v:
                vm_ip += str(ip) + ','
        ip = vm_ip.rstrip(',')
        return ip

    @staticmethod
    def get_target_ip(target):
        index = CONF.host_mgmt_ping.compute_hosts.index(target)
        if len(CONF.host_mgmt_ping.management_network_ip) > index:
            return CONF.host_mgmt_ping.management_network_ip[index]
        else:
            return None

    @staticmethod
    def reset_state(n_client, ori_state, current_state):
        LOG.info("Stating reset state for servers we previously "
                 "collected which failed perform evacuation")
        for uuid, state in current_state.items():
            if state != ori_state[uuid]:
                if ori_state[uuid] == u'active':
                    LOG.info("Reset state of %s from current state: "
                             "%s to origin state: active" % (uuid, state))
                    n_client.servers.reset_state(uuid, 'active')
                elif ori_state[uuid] == u'error':
                    LOG.info("Reset state of %s from current state: "
                             "%s to origin state: error" % (uuid, state))
                    n_client.servers.reset_state(uuid, 'error')
                elif ori_state[uuid] == u'stopped':
                    LOG.info("The origin state of %s is stopped, but current "
                             "is %s, we set it to error" % (uuid, state))
                    n_client.servers.reset_state(uuid, 'error')
                else:
                    LOG.info("The origin state of %s is %s, current is %s, so"
                             " we do not to reset the state" % (
                                 uuid, ori_state[uuid], state))
