"""Support for repeating alerts when conditions are met."""
import asyncio
import logging
from datetime import timedelta

import voluptuous as vol

import homeassistant.helpers.config_validation as cv
from homeassistant.const import (
    CONF_ENTITY_ID,
    STATE_IDLE,
    CONF_NAME,
    CONF_STATE,
    STATE_ON,
    STATE_OFF,
    SERVICE_TURN_ON,
    SERVICE_TURN_OFF,
    SERVICE_TOGGLE,
    ATTR_ENTITY_ID,
)
from homeassistant.helpers import service, event
from homeassistant.helpers.entity import ToggleEntity
from homeassistant.helpers.script import Script
from homeassistant.util.dt import now
from .const import (
    DOMAIN,
    ENTITY_ID_FORMAT,
    CONF_CAN_ACK,
    CONF_REPEAT,
    CONF_SKIP_FIRST,
    CONF_TRIGGERED,
    CONF_DONE,
    DEFAULT_CAN_ACK,
    DEFAULT_SKIP_FIRST,
)

_LOGGER = logging.getLogger(__name__)

ALERT_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_NAME): cv.string,
        vol.Required(CONF_ENTITY_ID): cv.entity_id,
        vol.Required(CONF_STATE, default=STATE_ON): cv.string,
        vol.Required(CONF_REPEAT): vol.All(cv.ensure_list, [vol.Coerce(float)]),
        vol.Required(CONF_CAN_ACK, default=DEFAULT_CAN_ACK): cv.boolean,
        vol.Required(CONF_SKIP_FIRST, default=DEFAULT_SKIP_FIRST): cv.boolean,
        vol.Optional(CONF_TRIGGERED): cv.SCRIPT_SCHEMA,
        vol.Optional(CONF_DONE): cv.SCRIPT_SCHEMA,
    }
)

CONFIG_SCHEMA = vol.Schema({DOMAIN: cv.schema_with_slug_keys(ALERT_SCHEMA)})

ALERT_SERVICE_SCHEMA = vol.Schema({vol.Required(ATTR_ENTITY_ID): cv.entity_ids})


def is_on(hass, entity_id):
    """Return if the alert is firing and not acknowledged."""
    return hass.states.is_state(entity_id, STATE_ON)


async def async_setup(hass, config):
    """Set up the Alert component."""
    entities = []

    for object_id, cfg in config[DOMAIN].items():
        if not cfg:
            cfg = {}

        name = cfg.get(CONF_NAME)
        watched_entity_id = cfg.get(CONF_ENTITY_ID)
        alert_state = cfg.get(CONF_STATE)
        repeat = cfg.get(CONF_REPEAT)
        skip_first = cfg.get(CONF_SKIP_FIRST)
        can_ack = cfg.get(CONF_CAN_ACK)
        triggered_action = cfg.get[CONF_TRIGGERED]
        done_action = cfg.get[CONF_DONE]

        entities.append(
            Alert(
                hass,
                object_id,
                name,
                watched_entity_id,
                alert_state,
                repeat,
                skip_first,
                can_ack,
                triggered_action,
                done_action,
            )
        )

    if not entities:
        return False

    async def async_handle_alert_service(service_call):
        """Handle calls to alert services."""
        alert_ids = await service.async_extract_entity_ids(hass, service_call)

        for alert_id in alert_ids:
            for alert in entities:
                if alert.entity_id != alert_id:
                    continue

                alert.async_set_context(service_call.context)
                if service_call.service == SERVICE_TURN_ON:
                    await alert.async_turn_on()
                elif service_call.service == SERVICE_TOGGLE:
                    await alert.async_toggle()
                else:
                    await alert.async_turn_off()

    # Setup service calls
    hass.services.async_register(
        DOMAIN,
        SERVICE_TURN_OFF,
        async_handle_alert_service,
        schema=ALERT_SERVICE_SCHEMA,
    )
    hass.services.async_register(
        DOMAIN, SERVICE_TURN_ON, async_handle_alert_service, schema=ALERT_SERVICE_SCHEMA
    )
    hass.services.async_register(
        DOMAIN, SERVICE_TOGGLE, async_handle_alert_service, schema=ALERT_SERVICE_SCHEMA
    )

    tasks = [alert.async_update_ha_state() for alert in entities]
    if tasks:
        await asyncio.wait(tasks)

    return True


class Alert(ToggleEntity):
    """Representation of an alert."""

    def __init__(
        self,
        hass,
        entity_id,
        name,
        watched_entity_id,
        state,
        repeat,
        skip_first,
        can_ack,
        triggered_action,
        done_action,
    ):
        """Initialize the alert."""
        self.hass = hass
        self._name = name
        self._alert_state = state
        self._skip_first = skip_first
        self._can_ack = can_ack

        self._triggered_script = Script(hass, triggered_action)
        self._done_script = Script(hass, done_action)

        self._delay = [timedelta(minutes=val) for val in repeat]
        self._next_delay = 0

        self._triggered = False
        self._ack = False
        self._cancel = None
        self._execute_done_script = False
        self.entity_id = ENTITY_ID_FORMAT.format(entity_id)

        event.async_track_state_change(
            hass, watched_entity_id, self.watched_entity_change
        )

    @property
    def name(self):
        """Return the name of the alert."""
        return self._name

    @property
    def should_poll(self):
        """HASS need not poll these entities."""
        return False

    @property
    def state(self):
        """Return the alert status."""
        if self._triggered:
            if self._ack:
                return STATE_OFF
            return STATE_ON
        return STATE_IDLE

    @property
    def hidden(self):
        """Hide the alert when it is not firing."""
        return not self._can_ack or not self._triggered

    async def watched_entity_change(self, entity, from_state, to_state):
        """Determine if the alert should start or stop."""
        _LOGGER.debug("Watched entity (%s) has changed", entity)
        if to_state.state == self._alert_state and not self._triggered:
            await self.begin_alerting()
        if to_state.state != self._alert_state and self._triggered:
            await self.end_alerting()

    async def begin_alerting(self):
        """Begin the alert procedures."""
        _LOGGER.debug("Beginning Alert: %s", self._name)
        self._ack = False
        self._triggered = True
        self._next_delay = 0

        if not self._skip_first:
            await self._trigger()
        else:
            await self._schedule_trigger()

        self.async_schedule_update_ha_state()

    async def end_alerting(self):
        """End the alert procedures."""
        _LOGGER.debug("Ending Alert: %s", self._name)
        self._cancel()
        self._ack = False
        self._triggered = False
        if self._execute_done_script:
            await self._done()
        self.async_schedule_update_ha_state()

    async def _schedule_trigger(self):
        """Schedule a trigger."""
        delay = self._delay[self._next_delay]
        next_msg = now() + delay
        self._cancel = event.async_track_point_in_time(
            self.hass, self._trigger, next_msg
        )
        self._next_delay = min(self._next_delay + 1, len(self._delay) - 1)

    async def _trigger(self, *args):
        """Fire the triggered action."""
        if not self._triggered:
            return

        if not self._ack:
            _LOGGER.info("Triggered alert: %s", self._name)
            self._execute_done_script = True

            self._execute_script(self._triggered_script)
        await self._schedule_trigger()

    async def _done(self, *args):
        """Fire the done action."""
        _LOGGER.info("Done alert: %s", self._name)
        self._execute_done_script = False
        self._execute_script(self._done_script)

    async def _execute_script(self, script):
        if script is None:
            return

        await script.async_run(context=self._context)

    async def async_turn_on(self, **kwargs):
        """Async Reset alert."""
        _LOGGER.debug("Reset Alert: %s", self._name)
        self._ack = False
        await self.async_update_ha_state()

    async def async_turn_off(self, **kwargs):
        """Async Acknowledge alert."""
        _LOGGER.debug("Acknowledged Alert: %s", self._name)
        self._ack = True
        await self.async_update_ha_state()

    async def async_toggle(self, **kwargs):
        """Async toggle alert."""
        if self._ack:
            return await self.async_turn_on()
        return await self.async_turn_off()
