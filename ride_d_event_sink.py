# @author: Kyle Benson
# (c) Kyle Benson 2017

import logging
from threading import Lock

log = logging.getLogger(__name__)

from ride.ride_d import RideD

from scale_client.event_sinks.event_sink import ThreadedEventSink
from scale_client.networks.coap_server import CoapServer
from scale_client.networks.coap_client import CoapClient
from scale_client.networks.util import DEFAULT_COAP_PORT, coap_response_success, coap_code_to_name, CoapCodes
from seismic_alert_common import *

# We typically wait until on_start to actually build RideD since it takes a while due to
# creating/updating the topology_manager, but you can selectively have it start right away.
# This is mostly for type hinting to an IDE that self.rided can be of type RideD
BUILD_RIDED_IN_INIT = False

class RideDEventSink(ThreadedEventSink):
    """
    An EventSink that delivers events using the RIDE-D middleware for resilient IP multicast-based publishing.
    """

    def __init__(self, broker,
                 # RideD parameters
                 # TODO: sublcass RideD in order to avoid code repetition here for extracting parameters?
                 dpid, addresses, topology_mgr='onos', ntrees=2,
                 tree_choosing_heuristic='importance', tree_construction_algorithm=('red-blue',),
                 # TODO: replace this with an API...
                 publishers=None,
                 # XXX: rather than running a separate service that would intercept incoming publications matching the
                 # specified flow for use in the STT, we simply wait for seismic picks and use them as if they're
                 # incoming packets.  This ignores other potential packets from those hosts, but this will have to do
                 # for now since running such a separated service would require more systems programming than this...
                 subscriptions=(SEISMIC_PICK_TOPIC,),
                 maintenance_interval=2,
                 multicast=True, port=DEFAULT_COAP_PORT, topics_to_sink=(SEISMIC_ALERT_TOPIC,), **kwargs):
        """
        See also the parameters for RideD constructor!

        :param broker:
        :param address_pool: iterable of IP addresses (formatted as strings) that can be used to register multicast trees
        :param port: port number to send events to (NOTE: we expect all subscribers to listen on the same port OR
        for you to configure the flow rules to convert this port to the expected one before delivery to subscriber)
        :param topics_to_sink: a SensedEvent whose topic matches one in this list will be
        resiliently multicast delivered; others will be ignored
        :param maintenance_interval: seconds between running topology updates and reconstructing MDMTs if necessary,
        accounting for topology changes or new/removed subscribers
        :param kwargs:
        """
        super(RideDEventSink, self).__init__(broker, subscriptions=subscriptions, **kwargs)

        # Catalogue active subscribers' host addresses (indexed by topic with value being a set of subscribers)
        self.subscribers = dict()

        self.port = port
        self.topics_to_sink = set(topics_to_sink)
        self.maintenance_interval = maintenance_interval

        # If we need to do anything with the server right away or expect some logic to be called
        # that will not directly check whether the server is running currently, we should wait
        # for a CoapServerRunning event before accessing the actual server.
        # NOTE: make sure to do this here, not on_start, as we currently only send the ready notification once!
        ev = CoapServer.CoapServerRunning(None)
        self.subscribe(ev, callback=self.__class__.__on_coap_ready)

        # Store parameters for RideD resilient multicast middleware; we'll actually build it later since it takes a while...
        self.use_multicast = multicast
        if not self.use_multicast:
            self.rided = None
        elif not BUILD_RIDED_IN_INIT:
            self.rided = dict(topology_mgr=topology_mgr, dpid=dpid, addresses=addresses, ntrees=ntrees,
                              tree_choosing_heuristic=tree_choosing_heuristic, tree_construction_algorithm=tree_construction_algorithm)
        else:
            self.rided = RideD(topology_mgr=topology_mgr, dpid=dpid, addresses=addresses, ntrees=ntrees,
                               tree_choosing_heuristic=tree_choosing_heuristic, tree_construction_algorithm=tree_construction_algorithm)

        # TODO: remove this when we move it to RIDE-C or to an API...
        self.publishers = publishers

        # Use a single client to connect with each server
        # COAPTHON-SPECIFIC: unclear that we'd be able to do this in all future versions...
        # NOTE: we specify a dummy server_hostname because we'll explicitly set it each time we use the client,
        # but it has to be a valid one to avoid causing an error...
        self.coap_client = CoapClient(server_hostname=addresses[0], server_port=self.port, confirmable_messages=not self.use_multicast)

        # Use thread locks to prevent simultaneous write access to data structures due to e.g.
        # handling multiple simultaneous subscription registrations.
        self.__subscriber_lock = Lock()


    def __maintain_topology(self):
        """Runs periodically to check for topology updates, reconstruct the MDMTs if necessary, and update flow
        rules to account for these topology changes or newly-joined/leaving subscribers."""

        # ENHANCE: only update the necessary changes: old subscribers are easy to trim, new ones could be added directly,
        # and topologies could be compared for differences (though that's probably about the same work as just refreshing the whole thing)
        # TODO: probably need to lock rided during this so we don't e.g. send_event to an MDMT that's currently being reconfigured.... maybe that's okay though?
        self.rided.update()

    def on_start(self):
        """
        Build and configure the RideD middleware
        """
        # TODO: probably run this in the background?

        if self.rided is not None:
            if not BUILD_RIDED_IN_INIT:
                assert isinstance(self.rided, dict)
                self.rided = RideD(**self.rided)
            assert isinstance(self.rided, RideD)

            # TODO: background instead of periodic?
            self.timed_call(self.maintenance_interval, self.__class__.__maintain_topology, repeat=True)

            #### set static routes for data Collection (ride-c)
            # TODO: change the paths we set based on new combined ride-c/d experiments???
            # TODO: move this to RideD?  Or RideC?
            assert self.publishers is not None
            log.info("setting static routes for publishers")
            # HACK: need to populate with pubs/subs so we just do this manually rather
            # than rely on a call to some REST API server/data exchange agent.
            for pub in self.publishers:
                # HACK: we get the shortest path (as per networkx) and set that as a static route
                # to prevent the controller from changing the path later since we don't dynamically
                # update the routes currently.
                try:
                    route = self.rided.topology_manager.get_path(pub, self.rided.dpid)
                    flow_rules = self.rided.topology_manager.build_flow_rules_from_path(route)
                    for r in flow_rules:
                        self.rided.topology_manager.install_flow_rule(r)
                    self.rided.set_publisher_route(pub, route)
                except BaseException as e:
                    log.warning("Route between publisher %s and server %s not found: skipping...\nError: %s" % (pub, self.rided.dpid, e))

                    # TODO: should we bring this back in?  and does this only raise KeyError when NO subs reachable?  or if any unknown?
            #     # BUGFIX: if all subscribers are unreachable in the topology due to failure updates
            #     # propagating to the controller, we won't have registered any subs for the topic.
            #     try:
            #         self.rided.get_subscribers_for_topic(SEISMIC_ALERT_TOPIC)
            #         mdmts = self.rided.build_mdmts()[SEISMIC_ALERT_TOPIC]
            #         self.rided.install_mdmts(mdmts)
            #     except KeyError:
            #         log.error("No subscribers reachable by server!  Aborting...")
            #         exit(self.EXIT_CODE_NO_SUBSCRIBERS)

        super(RideDEventSink, self).on_start()

    def __sendto(self, msg, topic, address, port=None):
        """
        Sends msg to the specified address using CoAP.  topic is used to define the path of the CoAP
        resource we PUT the msg in.
        NOTE: this is a synchronous operation and waits for a response if not in multicast mode
        :param msg:
        :param topic:
        :param address:
        :param port:
        :return:
        """
        if port is None:
            port = self.port

        # TODO: don't hardcode this...
        path = "/events/%s" % topic

        # By setting the 'server' attribute, we're telling the client what destination to use.
        # ENHANCE: COAPTHON-SPECIFIC: should probably make some @properties to keep these in line
        self.coap_client.server = (address, port)

        # Use async mode to send this message as otherwise sending a bunch of them can lead to a back log...
        self.coap_client.put(path=path, payload=msg, callback=self.__put_event_callback)

        log.debug("RIDE-D message sent: topic=%s ; address=%s" % (topic, address))

    def __put_event_callback(self, response):
        """
        This callback handles the CoAP response for a PUT message.  Currently it just logs the success or failure.
        :param response:
        :type response: coapthon.messages.response.Response
        :return:
        """

        # XXX: when client closes the last response is a NoneType
        if response is None:
            return
        elif coap_response_success(response):
            log.debug("successfully sent alert!")
        elif response.code == CoapCodes.NOT_FOUND.number:
            log.debug("remote rejected PUT request for uncreated object: did you forget to add that resource?")
        else:
            log.error("failed to send aggregated events due to Coap error: %s" % coap_code_to_name(response.code))

    def send_event(self, event):
        """
        When charged with sending raw data, we will send the message as configured
        we'll actually choose the best MDMT for resilient multicast delivery."""

        topic = event.topic
        encoded_event = self.encode_event(event)
        log.debug("Sending event via RIDE-D with topic %s" % topic)

        # Send the event as we're configured to
        try:
            # Determine the best MDMT, get the destination associated with it, and send the event.
            if self.use_multicast:
                # if we ever encounter this, replace it with some real error handling...
                assert self.rided is not None, "woops!  Ride-D should be set up but it isn't..."
                address = self.rided.get_best_multicast_address(topic)
                self.__sendto(encoded_event, topic=topic, address=address)

            # Configured as unicast, so send a message to each subscriber individually
            else:
                for address in self.subscribers.get(topic, []):
                    self.__sendto(encoded_event, topic=topic, address=address)

            return True

        except IOError as e:
            log.error("failed to send event via CoAP PUT due to error: %s" % e)
            return False

    def on_event(self, event, topic):
        """
        HACK: any seismic picks we receive are treated as incoming publications for the purposes of updating the
        STT.  This clearly does not belong in a finalized version of the RideD middleware, which would instead
        intercept actual packets matching a particular flow and use them to update the STT.
        :param event:
        :type event: scale_client.core.sensed_event.SensedEvent
        :param topic:
        :return:
        """

        assert topic == SEISMIC_PICK_TOPIC, "received non-seismic event we didn't subscribe to! topic=%s" % topic

        if self.rided and not event.is_local:
            # Find the publishing host's IP address and use that to notify RideD
            publisher = event.source
            publisher = get_hostname_from_path(publisher)
            assert publisher is not None, "error processing publication with no source hostname: %s" % event.source
            # TODO: may need to wrap this with mutex
            self.rided.notify_publication(publisher, id_type='ip')

    def process_subscription(self, topic, host):
        """
        Handles a subscription request by adding the host to the current subscribers.
        Note that we don't collect a port number or protocol type as we currently assume it will be
        CoAP and its well-known port number.
        :param topic:
        :param host: IP address or hostname of subscribing host (likely taken from CoAP request)
        :return:
        """

        log.debug("processing RIDE-D subscription for topic '%s' by host '%s'" % (topic, host))
        with self.__subscriber_lock:
            self.subscribers.setdefault(topic, set()).add(host)

        if self.rided:
            # WARNING: supposedly we should only register subscribers that are reachable in our topology view or
            #  we'll cause errors later... we should try to handle those errors instead!
            try:
                # ENHANCE: handle port numbers? all ports will be same for our scenario and OF could convert them anyway so no hurry...
                host = self.rided.topology_manager.get_host_by_ip(host)
                # If we can't find a path, how did we even get this subscription?  Path failed after it was sent?
                self.rided.topology_manager.get_path(host, self.rided.dpid)
                with self.__subscriber_lock:
                    self.rided.add_subscriber(host, topic_id=SEISMIC_ALERT_TOPIC)
            except BaseException as e:
                log.warning("Route between subscriber %s and server %s not found: skipping...\nError: %s" % (host, self.rided.dpid, e))
                return False

        return True

    def __on_coap_ready(self, server):
        """
        Register a CoAP API endpoint for subscribers to register their subscriptions through.
        :param CoapServer server:
        :return:
        """

        if self.use_multicast:
            # TODO: if we ever encounter this, we should delay registering the subscriptions API until after ride-d is setup
            # maybe we could just defer the arriving subscription by not sending a response?
            assert self.rided is not None, "woops coap is set up but ride-d isn't!!"

        # ENHANCE: could save server name to make sure we've got the right one her?
        # if self._server_name is None or self._server_name == server.name:
        self._server = server

        def __process_coap_subscription(coap_request, coap_resource):
            """
            Extract the relevant subscription information from the CoAP request object and pass it along to self.process_subscription()
            :param coap_request:
            :type coap_request: coapthon.messages.request.Request
            :param coap_resource:
            :return:
            """
            host, port = coap_request.source
            payload = coap_request.payload
            # ENHANCE: check the content-type?
            topic = payload
            # TODO: remove this hack later
            assert topic == SEISMIC_ALERT_TOPIC, "unrecognized subscription topic %s" % topic

            if self.process_subscription(topic, host):
                return coap_resource
            else:
                return False

        # ENHANCE: how to handle an unsubscribe?
        path = SUBSCRIPTION_API_PATH

        server.register_api(path, name="%s subscription registration" % SEISMIC_ALERT_TOPIC,
                            post_callback=__process_coap_subscription, allow_children=True)

    def check_available(self, event):
        """We only deliver events whose topic matches those that have been registered
         with RIDE-D and currently have subscribers."""
        return event.topic in self.topics_to_sink and event.topic in self.subscribers

    def on_stop(self):
        """Close any open network connections e.g. CoapClient"""
        self.coap_client.close()
        super(RideDEventSink, self).on_stop()