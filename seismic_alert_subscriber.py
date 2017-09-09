# @author: Kyle Benson
# (c) Kyle Benson 2016

import logging
log = logging.getLogger(__name__)

from scale_client.networks.util import coap_response_success, coap_code_to_name
from scale_client.networks.coap_client import CoapClient
from scale_client.networks.coap_server import CoapServer

from scale_client.core.application import Application
from seismic_alert_common import *

import json
import time


class SeismicAlertSubscriber(Application):
    """
    Testing application that receives 'seismic_alert' SensedEvents, which are multiple 'picks' aggregated together.
    It records when these alerts are received, how many duplicates arrive, and writes out these statistics on_stop()
    """

    # TODO: should we really be locally subscribing to anything here?
    def __init__(self, broker, remote_broker=None, output_file="output_events_rcvd", subscriptions=(SEISMIC_ALERT_TOPIC,), **kwargs):
        """
        :param broker:
        :param remote_broker: address/hostname of remote broker we'll subscribe with
        :param output_file:
        :param subscriptions:
        :param kwargs:
        """
        super(SeismicAlertSubscriber, self).__init__(broker, subscriptions=subscriptions, **kwargs)

        # Stores received UNIQUE events indexed by their 'id'
        # Includes the time they were received at and the total # copies
        self.events_rcvd = dict()

        self.output_file = output_file

        self.remote_broker = remote_broker
        if remote_broker is None:
            raise ValueError("remote_broker hostname/address must be specified for subscribing to work properly!")

        # Need to know when a CoapServer is running so we can properly subscribe to the remote and
        # open an endpoint on the server for receiving alert publications.
        ev = CoapServer.CoapServerRunning(None)
        self.subscribe(ev, callback=self.__class__.__on_coap_ready)

    def on_event(self, event, topic):
        """
        Receive an alert SensedEvent and process the event data (aggregated picks) inside its payload by
        recording the source of each pick, the time the alert was received, and incrementing the duplicate counters when necessary.
        :type event: scale_client.core.sensed_event.SensedEvent
        """

        # TODO: determine if this is thread-safe or if we need a Queue here too...

        if topic is None:
            topic = event.topic
        assert topic == SEISMIC_ALERT_TOPIC

        log.debug("processing alert: %s" % event.data)

        try:
            for ev_id, ev in event.data.items():
                if ev_id not in self.events_rcvd:
                    ev['time_rcvd'] = time.time()
                    ev['copies_rcvd'] = 1
                    self.events_rcvd[ev_id] = ev
                else:
                    self.events_rcvd[ev_id]['copies_rcvd'] += 1

        except (ValueError, KeyError) as e:
            log.error("Malformed seismic alert? err: %s" % e)

    def on_stop(self):
        """Records the received picks for consumption by another script
        that will analyze the resulting performance."""
        super(SeismicAlertSubscriber, self).on_stop()

        with open(self.output_file, "w") as f:
            f.write(json.dumps(self.events_rcvd))

    def __on_coap_ready(self, server):
        """
        Once the CoapServer is ready, we need to subscribe to the seismic alert topic
         via our specified remote as well as open up a local API endpoint (CoAP resource)
        for receiving the alerts.
        :return:
        """
        # TODO: store server name and check we get the right one?
        # ENHANCE: maybe this is a common pattern for scale modules that use coap resources?  really it's a remote_coap_subscribe(topic, cb=None)???  maybe this belongs in a RemotePubSubManager class to handle all these things...
        event = self.make_event(event_type=SEISMIC_ALERT_TOPIC, data=None)
        # TODO: not hard-code this
        path = '/events/%s' % SEISMIC_ALERT_TOPIC
        # NOTE: no one remote should POST only PUT; delete could recall/cancel an alert but we don't handle that...
        server.store_event(event, path, disable_post=True, disable_delete=True)

        self.timed_call(3, self.__class__.remote_subscribe, False, SEISMIC_ALERT_TOPIC, self.remote_broker)
        # self.remote_subscribe(SEISMIC_ALERT_TOPIC, remote_broker=self.remote_broker)

    # TODO: not hard-code subscriptions path
    def remote_subscribe(self, topic, remote_broker, path=SUBSCRIPTION_API_PATH):
        """
        Register subscription with remote_broker by sending a CoAP request to the specified path.
        :param path: string representing path part of subscription API URL; it should include a '%s' to be filled in with the topic
        """
        # ENHANCE: could use DEL to unsubscribe?

        try:
            path = path % topic
        # not a topic-formatting string? must be a raw path
        except TypeError:
            pass

        client = CoapClient(server_hostname=remote_broker)
        # TODO: how to ensure this will go through?  repeat it? esp. if remote server hasn't started yet...
        response = client.post(path=path, payload=topic)
        if not coap_response_success(response):
            log.error("failed to send subscription request due to Coap error: %s" % coap_code_to_name(response.code))
            # TODO: try again?

        client.close()