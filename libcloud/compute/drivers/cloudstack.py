import base64
import hashlib
import hmac
import time
import urllib

try:
    import json
except:
    import simplejson as json

from libcloud.common.base import ConnectionUserAndKey, Response
from libcloud.compute.base import Node, NodeDriver, NodeImage, NodeLocation, \
                                  NodeSize
from libcloud.compute.providers import Provider
from libcloud.compute.types import MalformedResponseError, NodeState

class CloudStackNode(Node):
    "Subclass of Node so we can expose our extension methods."

    def ex_allocate_public_ip(self):
        "Allocate a public IP and bind it to this node."
        return self.driver.ex_allocate_public_ip(self)

    def ex_release_public_ip(self, address):
        "Release a public IP that this node holds."
        return self.driver.ex_release_public_ip(self, address)

    def ex_add_ip_forwarding_rule(self, address, protocol, start_port,
                                  end_port=None):
        "Add a NAT/firewall forwarding rule for a port or ports."
        return self.driver.ex_add_ip_forwarding_rule(self, address, protocol,
                                                     start_port, end_port)

    def ex_delete_ip_forwarding_rule(self, rule):
        "Delete a NAT/firewall rule."
        return self.driver.ex_delete_ip_forwarding_rule(self, rule)

class CloudStackAddress(object):
    "A public IP address."

    def __init__(self, node, id, address):
        self.node = node
        self.id = id
        self.address = address

    def release(self):
        self.node.ex_release_public_ip(self)

    def __str__(self):
        return self.address

    def __eq__(self, other):
        return self.__class__ is other.__class__ and self.id == other.id

class CloudStackForwardingRule(object):
    "A NAT/firewall forwarding rule."

    def __init__(self, node, id, address, protocol, start_port, end_port=None):
        self.node = node
        self.id = id
        self.address = address
        self.protocol = protocol
        self.start_port = start_port
        self.end_port = end_port

    def delete(self):
        self.node.ex_delete_ip_forwarding_rule(self)

    def __eq__(self, other):
        return self.__class__ is other.__class__ and self.id == other.id

class CloudStackResponse(Response):
    def parse_body(self):
        try:
            body = json.loads(self.body)
        except:
            raise MalformedResponseError(
                "Failed to parse JSON",
                body=self.body,
                driver=self.connection.driver)
        return body

class CloudStackConnection(ConnectionUserAndKey):
    responseCls = CloudStackResponse

    def add_default_params(self, params):
        params['apiKey'] = self.user_id
        params['response'] = 'json'

        return params

    def pre_connect_hook(self, params, headers):
        signature = [(k.lower(), v) for k, v in params.items()]
        signature.sort(key=lambda x: x[0])
        signature = urllib.urlencode(signature)
        signature = signature.lower().replace('+', '%20')
        signature = hmac.new(self.key, msg=signature, digestmod=hashlib.sha1)
        params['signature'] = base64.b64encode(signature.digest())

        return params, headers

    def _sync_request(self, command, **kwargs):
        "Make a synchronous API request. These return immediately."

        kwargs['command'] = command
        result = self.request(self.driver.path, params=kwargs).object
        command = command.lower() + 'response'
        if command not in result:
            raise MalformedResponseError(
                "Unknown response format",
                body=result.body,
                driver=self.driver)
        result = result[command]
        return result

    def _async_request(self, command, **kwargs):
        """Make an asynchronous API request.

        These requests return a job_id which must be polled until it
        completes."""

        result = self._sync_request(command, **kwargs)
        job_id = result['jobid']
        success = True

        while True:
            result = self._sync_request('queryAsyncJobResult', jobid=job_id)
            if result.get('jobstatus', 0) == 0:
                continue
            time.sleep(self.async_poll_frequency)

        if result['jobstatus'] == 2:
            success = False
        else:
            result = result['jobresult']

        return success, result

class CloudStackNodeDriver(NodeDriver):
    """Driver for the CloudStack API.

    @cvar host: The host where the API can be reached.
    @cvar path: The path where the API can be reached.
    @cvar async_poll_frequency: How often (in seconds) to poll for async
                                job completion.
    @type async_poll_frequency: C{int}"""

    host = None
    path = None
    async_poll_frequency = 1

    NODE_STATE_MAP = {
        'Running': NodeState.RUNNING,
        'Starting': NodeState.REBOOTING,
        'Stopped': NodeState.TERMINATED,
        'Stopping': NodeState.TERMINATED
    }

    connectionCls = CloudStackConnection

    def __init__(self, key, secret=None, secure=True, host=None, port=None):
        host = host or self.host
        super(CloudStackNodeDriver, self).__init__(key, secret, secure, host,
                                                   port)

    def _sync_request(self, command, **kwargs):
        return self.connection._sync_request(command, **kwargs)

    def _async_request(self, command, **kwargs):
        return self.connection._async_request(command, **kwargs)

    def list_images(self, location=None):
        args = {
            'templatefilter': 'executable'
        }
        if location is not None:
            args['zoneid'] = location.id
        imgs = self._sync_request('listTemplates', **args)
        images = []
        for img in imgs['template']:
            images.append(NodeImage(img['id'], img['name'], self, {
                'hypervisor': img['hypervisor'],
                'format': img['format'],
                'os': img['ostypename'],
            }))
        return images

    def list_locations(self):
        locs = self._sync_request('listZones')
        locations = []
        for loc in locs['zone']:
            locations.append(NodeLocation(loc['id'], loc['name'], 'AU', self))
        return locations

    def list_nodes(self):
        vms = self._sync_request('listVirtualMachines')
        addrs = self._sync_request('listPublicIpAddresses')

        public_ips = {}
        for addr in addrs['publicipaddress']:
            if 'virtualmachineid' not in addr:
                continue
            vm_id = addr['virtualmachineid']
            if vm_id not in public_ips:
                public_ips[vm_id] = {}
            public_ips[vm_id][addr['ipaddress']] = addr['id']

        nodes = []

        for vm in vms.get('virtualmachine', []):
            node = CloudStackNode(
                id=vm['id'],
                name=vm.get('displayname', None),
                state=self.NODE_STATE_MAP[vm['state']],
                public_ip=public_ips.get(vm['id'], {}).keys(),
                private_ip=[x['ipaddress'] for x in vm['nic']],
                driver=self,
                extra={
                    'zoneid': vm['zoneid'],
                }
            )

            addrs = public_ips.get(vm['id'], {}).items()
            addrs = [CloudStackAddress(node, v, k) for k, v in addrs]
            node.extra['ip_addresses'] = addrs

            rules = []
            for addr in addrs:
                result = self._sync_request('listIpForwardingRules')
                for r in result.get('ipforwardingrule', []):
                    rule = CloudStackForwardingRule(node, r['id'], addr,
                                                    r['protocol'].upper(),
                                                    r['startport'],
                                                    r['endport'])
                    rules.append(rule)
            node.extra['ip_forwarding_rules'] = rules

            nodes.append(node)

        return nodes

    def list_sizes(self, location=None):
        szs = self._sync_request('listServiceOfferings')
        sizes = []
        for sz in szs['serviceoffering']:
            sizes.append(NodeSize(sz['id'], sz['name'], sz['memory'], 0, 0,
                                  0, self))
        return sizes

    def create_node(self, name, size, image, location=None, **kwargs):
        if location is None:
            location = self.list_locations()[0]

        networks = self._sync_request('listNetworks')
        network_id = networks['network'][0]['id']

        success, result = self._async_request('deployVirtualMachine',
            name=name,
            displayname=name,
            serviceofferingid=size.id,
            templateid=image.id,
            zoneid=location.id,
            networkids=network_id,
        )
        if not success:
            fail()

        node = result['jobresult']['virtualmachine']

        return Node(
            id=node['id'],
            name=node['displayname'],
            state=self.NODE_STATE_MAP[node['state']],
            public_ip=[],
            private_ip=[x['ipaddress'] for x in node['nic']],
            driver=self,
            extra={
                'zoneid': location.id,
                'ip_addresses': [],
                'forwarding_rules': [],
            }
        )

    def destroy_node(self, node):
        success, _ = self._async_request('destroyVirtualMachine', id=node.id)
        return success

    def reboot_node(self, node):
        success, _ = self._async_request('rebootVirtualMachine', id=node.id)
        return success

    def ex_allocate_public_ip(self, node):
        "Allocate a public IP and bind it to a node."

        zoneid = node.extra['zoneid']
        success, addr = self._async_request('associateIpAddress', zoneid=zoneid)
        if not success:
            return None
        addr = addr['ipaddress']
        result = self._sync_request('enableStaticNat', virtualmachineid=node.id,
                                   ipaddressid=addr['id'])
        if result.get('success', '').lower() != 'true':
            return None

        node.public_ip.append(addr['ipaddress'])
        addr = CloudStackAddress(node, addr['id'], addr['ipaddress'])
        node.extra['ip_addresses'].append(addr)
        return addr

    def ex_release_public_ip(self, node, address):
        "Release a public IP."

        node.extra['ip_addresses'].remove(address)
        node.public_ip.remove(address.address)

        self._async_request('disableStaticNat', ipaddressid=address.id)
        success, _ = self._async_request('disassociateIpAddress',
                                         id=address.id)
        return success

    def ex_add_ip_forwarding_rule(self, node, address, protocol,
                                  start_port, end_port=None):
        "Add a NAT/firewall forwarding rule."

        protocol = protocol.upper()
        if protocol not in ('TCP', 'UDP'):
            return None

        args = {
            'ipaddressid': address.id,
            'protocol': protocol,
            'startport': int(start_port)
        }
        if end_port is not None:
            args['endport'] = int(end_port)

        success, result = self._async_request('createIpForwardingRule', **args)
        result = result['ipforwardingrule']
        rule = CloudStackForwardingRule(node, result['id'], address,
                                        protocol, start_port, end_port)
        node.extra['ip_forwarding_rules'].append(rule)
        return rule

    def ex_delete_ip_forwarding_rule(self, node, rule):
        "Remove a NAT/firewall forwading rule."

        node.extra['ip_forwarding_rules'].remove(rule)
        success, _ = self._async_request('deleteIpForwardingRule', id=rule.id)
        return success
