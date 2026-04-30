#! /usr/bin/env python
# -*- coding: utf-8 -*-
####################
# Rain Machine Indigo Server Plugin 2
#
# Copyright (c) 2018-2023 Geoff Harris
#
# All rights reserved. This program and the accompanying materials
# are made available under the terms of the MIT Public License
# which accompanies this distribution, and is available at
# https://github.com/geoffsharris/RainMachine2/blob/main/LICENSE
#
#
# Contributors:
# Geoff Harris
# Huge thank you to Aaron Bach for writing the Regenmaschine library that this plugin uses
# https://github.com/bachya
#
import os
import sys
import time

# Indigo 2025.2 uses Python 3.13. This build uses a dependency-free
# RainMachine client in rm_sync_client.py instead of regenmaschine/aiohttp.
# Indigo can exec plugin.py without defining __file__, so resolve defensively.
def _rainmachine_plugin_dir():
    try:
        path = __file__
    except NameError:
        path = None

    candidates = []
    if path:
        candidates.append(os.path.dirname(os.path.abspath(path)))
    if sys.argv and sys.argv[0]:
        candidates.append(os.path.dirname(os.path.abspath(sys.argv[0])))
    candidates.append(os.getcwd())

    for candidate in candidates:
        if candidate and os.path.exists(os.path.join(candidate, "plugin.py")):
            return candidate
    return os.getcwd()

PLUGIN_DIR = _rainmachine_plugin_dir()
LIB_DIR = os.path.join(PLUGIN_DIR, "lib")
if os.path.isdir(LIB_DIR) and LIB_DIR not in sys.path:
    sys.path.insert(0, LIB_DIR)

try:
    import indigo
except ImportError:
    pass

import asyncio
from rm_sync_client import Client



################################################################################
class Plugin(indigo.PluginBase):
################################################################################

    def __init__(self, pluginId, pluginDisplayName, pluginVersion, pluginPrefs):
        indigo.PluginBase.__init__(self, pluginId, pluginDisplayName, pluginVersion, pluginPrefs)
        self.debug = False
        self.rainmachine_devices = {}
        self.client = Client()
        self.next_update_programs = time.time()
        self.update_queue = []
        self.controllers = {}
        self._startup_login_delay_done = False

################################################################################
    def __del__(self):
        indigo.PluginBase.__del__(self)

################################################################################
    def startup(self):
        self.debugLog(u"startup called")
        indigo.server.log("startup called")
        self.controllers = self.client.controllers

################################################################################
    def shutdown(self):
        self.debugLog(u"shutdown called")
        indigo.server.log("shutdown called")

    def _run_async(self, coro):
        """Run a coroutine from Indigo's synchronous plugin callbacks."""
        try:
            return asyncio.run(coro)
        except RuntimeError as e:
            indigo.server.log("RainMachine async runtime error: %s" % e, isError=True)
            raise

    def _maybe_startup_login_delay(self):
        """Delay only once after plugin startup to avoid first-login races."""
        if not self._startup_login_delay_done:
            self._startup_login_delay_done = True
            indigo.server.log("RainMachine startup settle delay before first login")
            self.sleep(3.0)

    def _mark_device_offline(self, device, message):
        indigo.server.log(message, isError=True)
        try:
            device.updateStateOnServer('device_online', value=False, clearErrorState=False)
            device.updateStateImageOnServer(indigo.kStateImageSel.SensorTripped)
        except Exception:
            pass

    def _get_device_controller(self, device):
        mac = device.pluginProps.get('deviceMAC', '')
        if mac and mac in self.controllers:
            return self.controllers[mac]
        if mac and mac in self.client.controllers:
            self.controllers = self.client.controllers
            return self.controllers[mac]
        raise Exception("RainMachine controller is not logged in or MAC is missing for device '%s'" % device.name)

################################################################################
    def deviceStartComm(self, device):
        self.debugLog("Starting device: " + device.name)
        if device.pluginProps["deviceMAC"]:
            indigo.server.log("device exists MAC: " + str(device.pluginProps["deviceMAC"]))
        else:
            indigo.server.log("new device")
        if device.id not in self.rainmachine_devices:
            indigo.server.log("Added Device id: " + str(device.id) + " to self.rainmachine_devices")
            self.mac_address = device.pluginProps["deviceMAC"]
        if device.id in self.rainmachine_devices:
            indigo.server.log("Existing Device id: " + str(device.id) + " : MAC = " + self.rainmachine_devices[device.id])
        self.rainmachine_devices[device.id] = device.pluginProps["deviceMAC"]
        indigo.server.log("Device Start Communication")
        self.user_name = device.pluginProps["username"]
        self.password = device.pluginProps["password"]
        self.connection_type = device.pluginProps["connectionType"]
        self.ip_address = device.pluginProps["ip_address"]
        self.port = device.pluginProps["port"]
        self.ssl = device.pluginProps["https"]
        self.debugLog("Started device: " + str(device.id))
        self.debugLog(str(device))
        if device.pluginProps["deviceMAC"] not in self.controllers:
            indigo.server.log("Needs to login")
            self._maybe_startup_login_delay()
            try:
                if self.connection_type == 'Local':
                    self._run_async(self.loginDevicesLocal(self.ip_address, self.password))
                elif self.connection_type == 'Cloud':
                    self._run_async(self.loginDevicesCloudWithRetry(self.user_name, self.password))
                else:
                    indigo.server.log("Unknown RainMachine connection type: " + str(self.connection_type), isError=True)
            except Exception as e:
                self._mark_device_offline(device, "RainMachine login failed for %s: %s" % (device.name, e))
                raise
        # Refresh controller cache after login/discovery. Some RainMachine/cloud logins
        # populate controllers only after the async load completes. If the device has
        # no saved MAC yet, use the first discovered controller and save it back to
        # the Indigo device props. This helps the "doesn't like to login first time" case.
        self.controllers = self.client.controllers
        saved_mac = device.pluginProps.get("deviceMAC", "")
        if not saved_mac and self.controllers:
            first_controller = next(iter(self.controllers.values()))
            saved_mac = getattr(first_controller, "mac", None) or next(iter(self.controllers.keys()))
            self.rainmachine_devices[device.id] = saved_mac
            localPropsCopy = dict(device.pluginProps)
            localPropsCopy["deviceMAC"] = saved_mac
            device.replacePluginPropsOnServer(localPropsCopy)
            indigo.server.log("Saved RainMachine MAC after login: " + str(saved_mac))

        if saved_mac in self.controllers:
            indigo.server.log("Device logged in")
        else:
            raise Exception("RainMachine login succeeded but controller MAC was not found. Known controllers: %s" % list(self.controllers.keys()))

        self.mac_address = self.rainmachine_devices[device.id]
        self.controller = self._get_device_controller(device)
        self.program_list = self._run_async(self.controller.programs.all())
        self.zone_list = self._run_async(self.controller.zones.all())
        dev = indigo.devices[device]
        localPropsCopy = dev.pluginProps
        localPropsCopy.update({'address':self.ip_address})
        dev.replacePluginPropsOnServer(localPropsCopy)

        # Update status on server: #
        device.updateStateImageOnServer(indigo.kStateImageSel.SensorOn)
        device.updateStateOnServer('device_online', value=True, clearErrorState=True)

################################################################################
    def deviceStopComm(self, device):
        self.debugLog("Stopping device: " + device.name)
        indigo.server.log("Device Stop Communication")
        if device.id in self.rainmachine_devices:
            device.updateStateImageOnServer(indigo.kStateImageSel.SensorTripped)
            device.updateStateOnServer('device_online', value=False, clearErrorState=True)
            del self.rainmachine_devices[device.id]

################################################################################
    async def update(self):

        self.logger.debug("Update Called")
        if self.update_queue:
            self.debugLog("queue has update")
            deviceId = int(self.update_queue.pop(0))
            self.debugLog("device id:" + str(deviceId))

            device = indigo.devices[deviceId]
            zone_data = await self.activeZone(deviceId)
            self.debugLog("device to update" + str(zone_data))
            counter = 0
            for kd in zone_data: # for kd in zone_data[u'zones']:
                if kd[u'state'] != 0:
                    self.debugLog("zone {} is on for {} seconds".format(kd[u'name'], (kd[u'remaining'])))
                    device.updateStateOnServer('current_zone', kd[u'name'], clearErrorState=True)
                    device.updateStateOnServer('minutes_left', int(round(kd[u'remaining'] / 60)),
                                               uiValue=str(int(round(kd[u'remaining'] / 60))) + " min",
                                               clearErrorState=True)
                    device.updateStateOnServer('active_watering', 'on', clearErrorState=True)
                    device.updateStateImageOnServer(indigo.kStateImageSel.SprinklerOn)
                    counter += 1
            if counter == 0:
                self.debugLog("no active zones")
                device.updateStateOnServer('current_zone', 'all off', clearErrorState=True)
                device.updateStateOnServer('minutes_left', 0, uiValue='0 min', clearErrorState=True)
                device.updateStateOnServer('active_watering', 'off', clearErrorState=True)
                device.updateStateImageOnServer(indigo.kStateImageSel.SensorOn)

            program_data = await self.activeProgram(deviceId)
            if program_data and program_data[0]['name'] != None:
                self.debugLog("program data : " + str(program_data))
                self.debugLog("program data available")
                for kd in program_data:  # for kd in program_data[u'programs']:
                    device.updateStateOnServer('current_program', kd['name'], clearErrorState=True)
            else:
                device.updateStateOnServer('current_program', 'none', clearErrorState=True)
                self.debugLog("program data should be none")

        if time.time() > self.next_update_programs:
            counter = 0
            for key, value in self.rainmachine_devices.items():
                deviceId = key
                device = indigo.devices[deviceId]
                zone_data = await self.activeZone(deviceId)
                for kd in zone_data: # for kd in zone_data[u'zones']:
                    if kd[u'state'] != 0:
                        #self.debugLog("zone {} is on for {} seconds".format(kd[u'name'], (kd[u'remaining'])))
                        device.updateStateOnServer('current_zone', kd[u'name'], clearErrorState=True)
                        device.updateStateOnServer('minutes_left', round(kd[u'remaining'] / 60),
                                                   uiValue = str(int(round(kd[u'remaining'] / 60))) + " min",
                                                   clearErrorState = True)
                        device.updateStateOnServer('active_watering', 'on', clearErrorState=True)
                        device.updateStateImageOnServer(indigo.kStateImageSel.SprinklerOn)
                        counter += 1
                if counter == 0:
                    device.updateStateOnServer('current_zone', 'all off', clearErrorState=True)
                    device.updateStateOnServer('minutes_left', 0, uiValue='0 min', clearErrorState=True)
                    device.updateStateOnServer('active_watering', 'off', clearErrorState=True)
                    device.updateStateImageOnServer(indigo.kStateImageSel.SensorOn)

                program_data = await self.activeProgram(deviceId)
                if program_data and program_data[0]['name'] != None:
                    self.debugLog("program data : " + str(program_data))
                    for kd in program_data: #for kd in program_data[u'programs']:
                        device.updateStateOnServer('current_program', kd['name'], clearErrorState=True)
                else:
                    device.updateStateOnServer('current_program', 'none', clearErrorState=True)
                    self.debugLog("program data should be none: " + str(program_data))

                flowmeter_data = await self.flowmeter(deviceId)
                if flowmeter_data:
                    self.debugLog("flowmeter data : " + str(flowmeter_data))
                    device.updateStateOnServer('flowmeter', (str(flowmeter_data['flowMeterWateringClicks']+flowmeter_data['flowMeterLeakClicks'])+' gal'), clearErrorState=True)
                else:
                    pass

            self.next_update_programs = time.time() + 60

################################################################################
    def runConcurrentThread(self):
        self.logger.debug(u"runConcurrentThread starting")
        try:
            while True:

                if time.time() > self.next_update_programs:
                    try:
                        self._run_async(self.update())
                    except Exception as e:
                        indigo.server.log("RainMachine update failed: %s" % e, isError=True)
                        self.next_update_programs = time.time() + 60

                self.sleep(10.0)

        except self.StopThread:
            self.logger.debug(u"runConcurrentThread ending")
            pass

    def stopConcurrentThread(self):
        self.stopThread = True

    #####################################
    #            Run Program            #
    #####################################
    def actionRunProgram(self, pluginAction):
        deviceId = int(pluginAction.props['indigo_rainmachine_controller'])
        device = indigo.devices[deviceId]
        controller = self._get_device_controller(device)
        self._run_async(controller.programs.start(pluginAction.props["ProgramValue"]))
        self.debugLog("Starting Program: " + str(pluginAction.props["ProgramValue"]))

        self.update_queue.append(deviceId)
        self._run_async(self.update())

    def actionStopProgram(self, pluginAction):
        deviceId = int(pluginAction.props['indigo_rainmachine_controller'])
        device = indigo.devices[deviceId]
        controller = self._get_device_controller(device)
        self._run_async(controller.programs.stop(pluginAction.props["ProgramValue"]))
        self.debugLog("Stopping Program: " + str(pluginAction.props["ProgramValue"]))

        self.update_queue.append(deviceId)
        self._run_async(self.update())

    #####################################
    #              Run Zones            #
    #####################################
    def actionRunZones(self, pluginAction):
        deviceId = int(pluginAction.props['indigo_rainmachine_controller'])
        device = indigo.devices[int(pluginAction.props['indigo_rainmachine_controller'])]
        controller = self._get_device_controller(device)
        self._run_async(controller.zones.start(pluginAction.props["ZoneValue"], pluginAction.props["zoneDuration"]))
        self.debugLog("Zone: " + str(pluginAction.props["ZoneValue"]) + " Time: " + str(pluginAction.props["zoneDuration"]))

        self.update_queue.append(deviceId)
        self._run_async(self.update())

    def actionStopZones(self, pluginAction):
        deviceId = int(pluginAction.props['indigo_rainmachine_controller'])
        device = indigo.devices[int(pluginAction.props['indigo_rainmachine_controller'])]
        controller = self._get_device_controller(device)
        self._run_async(controller.zones.stop(pluginAction.props["ZoneValue"]))
        self.debugLog("Stopping Zone: " + str(pluginAction.props["ZoneValue"]))

        self.update_queue.append(deviceId)
        self._run_async(self.update())

    def actionAllOff(self, pluginAction):
        deviceId = int(pluginAction.props['indigo_rainmachine_controller'])
        device = indigo.devices[int(pluginAction.props['indigo_rainmachine_controller'])]
        controller = self._get_device_controller(device)
        self._run_async(controller.watering.stop_all())
        self.debugLog("Stopping All Called")

        self.update_queue.append(deviceId)
        self._run_async(self.update())

    #########################################
    #  populate menus for zone and program  #
    #########################################

    def availableSchedules(self, filter="", valuesDict=None, typeId="", targetId=0):
        passed_schedule_list = []
        if 'indigo_rainmachine_controller' in valuesDict:
            device = indigo.devices[int(valuesDict['indigo_rainmachine_controller'])]
            controller = self._get_device_controller(device)
            self.program_list = self._run_async(controller.programs.all())
            passed_schedule_list = [(program_dict_key, program_dict_value["name"]) for program_dict_key, program_dict_value in self.program_list.items()]
            self.debugLog("indigo.device.id : " + str(valuesDict['indigo_rainmachine_controller']))
            self.debugLog("indigo.mac.id : " + str(device.pluginProps['deviceMAC']))
        return passed_schedule_list

    def availableZones(self, filter="", valuesDict=None, typeId="", targetId=0):
        passed_zone_list = []
        if 'indigo_rainmachine_controller' in valuesDict:
            device = indigo.devices[int(valuesDict['indigo_rainmachine_controller'])]
            controller = self._get_device_controller(device)
            self.zone_list = self._run_async(controller.zones.all())
            passed_zone_list = [(zone_dict_key, zone_dict_value["name"]) for zone_dict_key, zone_dict_value in self.zone_list.items()] #[(int(zone_dict["uid"]), zone_dict["name"]) for zone_dict in self.zone_list["zones"]]
        return passed_zone_list

    def availableDevices(self, filter="", valuesDict=None, typeId="", targetId=0):
        controller_list = [(controller.mac, controller.name) for controller in self.client.controllers.values()]
        return controller_list

    #################################
    #  login into existing devices  #
    #################################

    def loginDevices(self, valuesDict, typeId, devId):
        if valuesDict["connectionType"] == 'Local':
            ip_address = valuesDict["ip_address"]
            password = valuesDict["password"]
            self._run_async(self.loginDevicesLocal(ip_address, password))
        elif valuesDict["connectionType"] == 'Cloud':
            self._run_async(self.loginDevicesCloud(valuesDict, typeId, devId))
        else:
            indigo.server.log("Error in login")
        pass

    async def loginDevicesLocal(self, ip_address, password):
        await self._with_login_retry(self.client.load_local, ip_address, password)

    async def loginDevicesCloudWithRetry(self, username, password):
        await self._with_login_retry(self.client.load_remote, username, password)

    async def loginDevicesCloud(self, valuesDict, typeId, devId):
        if valuesDict["connectionType"] == 'Cloud':
            await self.loginDevicesCloudWithRetry(valuesDict["username"], valuesDict["password"])
        else:
            indigo.server.log("Error in login")
        pass

    async def _with_login_retry(self, login_func, *args):
        last_error = None
        max_attempts = 5
        for attempt in range(1, max_attempts + 1):
            try:
                if attempt > 1:
                    indigo.server.log("RainMachine login retry %d of %d" % (attempt, max_attempts))
                # Bound each attempt so a stuck cloud request does not block Indigo startup forever.
                await asyncio.wait_for(login_func(*args), timeout=30)
                self.controllers = self.client.controllers
                if self.controllers:
                    indigo.server.log("RainMachine login succeeded; found %d controller(s)" % len(self.controllers))
                return
            except Exception as e:
                last_error = e
                indigo.server.log("RainMachine login attempt %d failed: %s" % (attempt, e), isError=True)
                if attempt < max_attempts:
                    await asyncio.sleep(min(15, 2 * attempt))
        raise last_error

    ######################
    #  Updates UI menus  #
    ######################
    def menuChanged(self, valuesDict, typeId, devId):
        return valuesDict

    ######################
    #  Update functions  #
    ######################
    async def activeZone(self, deviceId):
        device = indigo.devices[deviceId]
        controller = self._get_device_controller(device)
        current_zone = await controller.zones.running()
        self.debugLog("active zone called")
        return current_zone

    async def activeProgram(self, deviceId):
        device = indigo.devices[deviceId]
        controller = self._get_device_controller(device)
        current_program = await controller.programs.running()
        self.debugLog("active program called")
        return current_program

    async def flowmeter(self, deviceId):
        device = indigo.devices[deviceId]
        controller = self._get_device_controller(device)
        flowmeter = await controller.watering.flowmeter()
        self.debugLog("flowmeter called")
        self.debugLog(str(flowmeter))
        return flowmeter

    ######################
    #  Debugger Toggle   #
    ######################
    def toggleDebugging(self):
        if self.debug:
            self.logger.info("Turning off debug logging")
            self.pluginPrefs["showDebugInfo"] = False
        else:
            self.logger.info("Turning on debug logging")
            self.pluginPrefs["showDebugInfo"] = True
        self.debug = not self.debug
