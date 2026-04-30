# -*- coding: utf-8 -*-
"""Small dependency-free RainMachine API client for Indigo.

This intentionally implements only the calls used by the RainMachine2 Indigo
plugin so the plugin no longer depends on regenmaschine/aiohttp under Indigo
2025.2 / Python 3.13.
"""

import json
import ssl
import time
import urllib.parse
import urllib.request

DEFAULT_LOCAL_PORT = 8080
DEFAULT_TIMEOUT = 30


class RainMachineError(Exception):
    pass


def _ssl_context():
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    try:
        ctx.minimum_version = ssl.TLSVersion.SSLv3
    except Exception:
        pass
    try:
        ctx.set_ciphers("DEFAULT:@SECLEVEL=0")
    except Exception:
        pass
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    ctx.options |= getattr(ssl, "OP_LEGACY_SERVER_CONNECT", 0x4)
    return ctx


def _join_url(base, endpoint):
    return base.rstrip("/") + "/" + endpoint.lstrip("/")


class Client:
    def __init__(self, request_timeout=DEFAULT_TIMEOUT, session=None):
        self.controllers = {}
        self._request_timeout = request_timeout
        self._ssl_context = _ssl_context()

    async def load_local(self, host, password, port=DEFAULT_LOCAL_PORT, use_ssl=True, skip_existing=True):
        controller = Controller(self, local=True, host=host, port=port, use_ssl=use_ssl)
        await controller.login_local(password)
        wifi_data = await controller.provisioning.wifi()
        mac = wifi_data.get("macAddress") or wifi_data.get("mac") or host
        if skip_existing and mac in self.controllers:
            return
        version_data = await controller.api.versions()
        controller.api_version = version_data.get("apiVer", "")
        controller.hardware_version = str(version_data.get("hwVer", ""))
        controller.mac = mac
        controller.software_version = version_data.get("swVer", "")
        try:
            controller.name = str(await controller.provisioning.device_name)
        except Exception:
            controller.name = host
        self.controllers[controller.mac] = controller

    async def load_remote(self, email, password, skip_existing=True):
        auth_resp = self._request(
            "post",
            "https://my.rainmachine.com/login/auth",
            json_body={"user": {"email": email, "pwd": password, "remember": 1}},
            use_ssl=True,
        )
        stage_1_token = auth_resp["access_token"]
        sprinklers_resp = self._request(
            "post",
            "https://my.rainmachine.com/devices/get-sprinklers",
            access_token=stage_1_token,
            json_body={"user": {"email": email, "pwd": password, "remember": 1}},
            use_ssl=True,
        )
        for sprinkler in sprinklers_resp.get("sprinklers", []):
            mac = sprinkler.get("mac")
            if not mac:
                continue
            if skip_existing and mac in self.controllers:
                continue
            controller = Controller(self, local=False, sprinkler_id=sprinkler["sprinklerId"])
            await controller.login_remote(stage_1_token, sprinkler["sprinklerId"], password)
            version_data = await controller.api.versions()
            controller.api_version = version_data.get("apiVer", "")
            controller.hardware_version = str(version_data.get("hwVer", ""))
            controller.mac = mac
            controller.name = str(sprinkler.get("name") or mac)
            controller.software_version = version_data.get("swVer", "")
            self.controllers[mac] = controller

    def _request(self, method, url, access_token=None, json_body=None, use_ssl=True):
        params = {}
        if access_token:
            params["access_token"] = access_token
        if params:
            sep = "&" if "?" in url else "?"
            url = url + sep + urllib.parse.urlencode(params)

        data = None
        headers = {"Content-Type": "application/json"}
        if json_body is not None:
            data = json.dumps(json_body).encode("utf-8")

        req = urllib.request.Request(url, data=data, headers=headers, method=method.upper())
        ctx = self._ssl_context if url.startswith("https://") and use_ssl else None
        try:
            with urllib.request.urlopen(req, timeout=self._request_timeout, context=ctx) as resp:
                raw = resp.read().decode("utf-8")
        except Exception as e:
            raise RainMachineError("Error requesting %s: %s" % (url, e))

        try:
            payload = json.loads(raw) if raw else {}
        except Exception as e:
            raise RainMachineError("Unable to parse JSON from %s: %s" % (url, e))

        # Local API error shape.
        if isinstance(payload, dict):
            if payload.get("statusCode", 0) not in (0, None):
                raise RainMachineError("RainMachine error %s: %s" % (payload.get("statusCode"), payload.get("message")))
            # Cloud API error shape.
            if payload.get("errorType"):
                raise RainMachineError("RainMachine cloud error: %s" % payload.get("errorType"))
        return payload


class Controller:
    def __init__(self, client, local=False, host=None, port=DEFAULT_LOCAL_PORT, use_ssl=True, sprinkler_id=None):
        self._client = client
        self._access_token = None
        self._access_token_expiration = 0
        self._use_ssl = use_ssl
        if local:
            scheme = "https" if use_ssl else "http"
            self._base_url = "%s://%s:%s/api/4" % (scheme, host, port)
        else:
            self._base_url = "https://api.rainmachine.com/%s/api/4" % sprinkler_id
        self.api_version = ""
        self.hardware_version = ""
        self.mac = ""
        self.name = ""
        self.software_version = ""
        self.api = API(self)
        self.programs = Program(self)
        self.watering = Watering(self)
        self.zones = Zone(self)
        self.provisioning = Provision(self)

    async def login_local(self, password):
        auth_resp = self._client._request(
            "post",
            _join_url(self._base_url, "auth/login"),
            json_body={"pwd": password, "remember": 1},
            use_ssl=self._use_ssl,
        )
        self._access_token = auth_resp["access_token"]
        self._access_token_expiration = time.time() + int(auth_resp.get("expires_in", 3600)) - 10

    async def login_remote(self, stage_1_access_token, sprinkler_id, password):
        auth_resp = self._client._request(
            "post",
            "https://my.rainmachine.com/devices/login-sprinkler",
            access_token=stage_1_access_token,
            json_body={"sprinklerId": sprinkler_id, "pwd": password},
            use_ssl=True,
        )
        self._access_token = auth_resp["access_token"]

    async def request(self, method, endpoint, json=None):
        if self._access_token_expiration and time.time() >= self._access_token_expiration:
            raise RainMachineError("RainMachine access token expired; reload/login required")
        return self._client._request(
            method,
            _join_url(self._base_url, endpoint),
            access_token=self._access_token,
            json_body=json,
            use_ssl=self._use_ssl,
        )


class API:
    def __init__(self, controller): self.controller = controller
    async def versions(self): return await self.controller.request("get", "apiVer")


class Provision:
    def __init__(self, controller): self.controller = controller
    @property
    async def device_name(self):
        data = await self.controller.request("get", "provision/name")
        return data.get("name", "RainMachine")
    async def wifi(self): return await self.controller.request("get", "provision/wifi")


class Program:
    def __init__(self, controller): self.controller = controller
    async def all(self, include_inactive=False):
        data = await self.controller.request("get", "program")
        programs = {}
        for program in data.get("programs", []):
            if include_inactive or program.get("active", True):
                programs[program["uid"]] = program
        return programs
    async def running(self):
        data = await self.controller.request("get", "watering/program")
        return data.get("programs", [])
    async def start(self, program_id):
        return await self.controller.request("post", "program/%s/start" % program_id, json={"pid": program_id})
    async def stop(self, program_id):
        return await self.controller.request("post", "program/%s/stop" % program_id, json={"pid": program_id})


class Zone:
    def __init__(self, controller): self.controller = controller
    async def all(self, include_inactive=False, details=False):
        data = await self.controller.request("get", "zone")
        zones = {}
        for zone in data.get("zones", []):
            if "active" not in zone:
                zone["active"] = True
            if include_inactive or zone.get("active", True):
                zones[zone["uid"]] = zone
        return zones
    async def running(self):
        data = await self.controller.request("get", "watering/zone")
        return data.get("zones", [])
    async def start(self, zone_id, seconds):
        return await self.controller.request("post", "zone/%s/start" % zone_id, json={"time": seconds, "zid": zone_id})
    async def stop(self, zone_id):
        return await self.controller.request("post", "zone/%s/stop" % zone_id, json={"zid": zone_id})


class Watering:
    def __init__(self, controller): self.controller = controller
    async def flowmeter(self): return await self.controller.request("get", "watering/flowmeter")
    async def stop_all(self): return await self.controller.request("post", "watering/stopall")
