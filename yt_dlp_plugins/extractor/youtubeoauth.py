import datetime
import importlib
import inspect
import json
import socket
import time
import urllib.parse
import uuid
from urllib.request import Request, urlopen

import yt_dlp.networking
from yt_dlp.extractor.common import InfoExtractor
from yt_dlp.extractor.youtube import YoutubeBaseInfoExtractor
from yt_dlp.utils import ExtractorError
from yt_dlp.utils.traversal import traverse_obj

_EXCLUDED_IES = ("YoutubeBaseInfoExtractor", "YoutubeTabBaseInfoExtractor")

YOUTUBE_IES = filter(
    lambda member: issubclass(member[1], YoutubeBaseInfoExtractor)
    and member[0] not in _EXCLUDED_IES,
    inspect.getmembers(
        importlib.import_module("yt_dlp.extractor.youtube"), inspect.isclass
    ),
)

__VERSION__ = "2024.10.29"

# YouTube TV (TVHTML5)
_CLIENT_ID = "861556708454-d6dlm3lh05idd8npek18k6be8ba3oc68.apps.googleusercontent.com"
_CLIENT_SECRET = "SboVhoG9s0rNafixCSGGKXAT"
_SCOPES = "http://gdata.youtube.com https://www.googleapis.com/auth/youtube"


def _execute_request(
    url, method=None, headers=None, data=None, timeout=socket._GLOBAL_DEFAULT_TIMEOUT
):
    base_headers = {"User-Agent": "Mozilla/5.0", "accept-language": "en-US,en"}
    if headers:
        base_headers.update(headers)
    if data and not isinstance(data, bytes):  # encode data for request
        data = bytes(json.dumps(data), encoding="utf-8")
    if url.lower().startswith("http"):
        request = Request(url, headers=base_headers, method=method, data=data)
    else:
        raise ValueError("Invalid URL")
    return urlopen(request, timeout=timeout)  # nosec


class YouTubeOAuth2Handler(InfoExtractor):
    def set_downloader(self, downloader):
        super().set_downloader(downloader)
        if downloader:
            downloader.write_debug(
                f"YouTube OAuth2 plugin version {__VERSION__}", only_once=True
            )
            downloader.report_warning(
                (
                    "The YouTube OAuth2 plugin is obsolete! Official support for logging into YouTube with OAuth in "
                    "yt-dlp was added in the 2024.10.22 release. \n"
                    "See  https://github.com/yt-dlp/yt-dlp/wiki/Extractors#logging-in-with-oauth  for how to use "
                    "the official OAuth implementation.\n\n"
                    "To uninstall the plugin:\n\n"
                    "pip: pip uninstall yt-dlp-youtube-oauth2\n"
                    "pipx: pipx uninject yt-dlp yt-dlp-youtube-oauth2\n"
                    "manual: delete the plugin zip or files where you installed them\n"
                ),
                only_once=True,
            )

    def store_token(self, token_data):
        self.cache.store("youtube-oauth2", "token_data", token_data)
        self._TOKEN_DATA = token_data

    def get_token(self):
        if not getattr(self, "_TOKEN_DATA", None):
            self._TOKEN_DATA = self.cache.load("youtube-oauth2", "token_data")
        return self._TOKEN_DATA

    def validate_token_data(self, token_data):
        return all(
            key in token_data
            for key in ("access_token", "expires", "refresh_token", "token_type")
        )

    def initialize_oauth(self):
        token_data = self.get_token()

        if token_data and not self.validate_token_data(token_data):
            self.report_warning("Invalid cached OAuth2 token data")
            token_data = None

        if not token_data:
            token_data = self.authorize()
            self.store_token(token_data)

        if (
            token_data["expires"]
            < datetime.datetime.now(datetime.timezone.utc).timestamp() + 60
        ):
            self.to_screen("Access token expired, refreshing")
            token_data = self.refresh_token(token_data["refresh_token"])
            self.store_token(token_data)

        return token_data

    def handle_oauth(self, request: yt_dlp.networking.Request):
        if not urllib.parse.urlparse(request.url).netloc.endswith("youtube.com"):
            return

        token_data = self.initialize_oauth()
        # These are only require for cookies and interfere with OAuth2
        request.headers.pop("X-Goog-PageId", None)
        request.headers.pop("X-Goog-AuthUser", None)
        # In case user tries to use cookies at the same time
        if "Authorization" in request.headers:
            self.report_warning(
                "Youtube cookies have been provided, but OAuth2 is being used."
                " If you encounter problems, stop providing Youtube cookies to yt-dlp."
            )
            request.headers.pop("Authorization", None)
            request.headers.pop("X-Origin", None)

        # Not even used anymore, should be removed from core...
        request.headers.pop("X-Youtube-Identity-Token", None)

        authorization_header = {
            "Authorization": f"{token_data['token_type']} {token_data['access_token']}"
        }
        request.headers.update(authorization_header)

    def refresh_token(self, refresh_token):
        token_response = self._download_json(
            "https://www.youtube.com/o/oauth2/token",
            video_id="oauth2",
            note="Refreshing OAuth2 Token",
            data=json.dumps(
                {
                    "client_id": _CLIENT_ID,
                    "client_secret": _CLIENT_SECRET,
                    "refresh_token": refresh_token,
                    "grant_type": "refresh_token",
                }
            ).encode(),
            headers={"Content-Type": "application/json", "__youtube_oauth__": True},
        )
        error = traverse_obj(token_response, "error")
        if error:
            self.report_warning(
                f"Failed to refresh access token: {error}. Restarting authorization flow"
            )
            return self.authorize()

        return {
            "access_token": token_response["access_token"],
            "expires": datetime.datetime.now(datetime.timezone.utc).timestamp()
            + token_response["expires_in"],
            "token_type": token_response["token_type"],
            "refresh_token": token_response.get("refresh_token", refresh_token),
        }

    def authorize(self):
        response_data = self._download_json(
            "https://oauth2.googleapis.com/device/code",
            video_id="oauth2",
            note="Initializing OAuth2 Authorization Flow",
            data=json.dumps(
                {
                    "client_id": _CLIENT_ID,
                    "scope": "https://www.googleapis.com/auth/youtube",
                }
            ).encode(),
            headers={
                "Content-Type": "application/json",
                "User-Agent": "Mozilla/5.0",
                "Accept": "application/json",
                "Accept-Language": "en-US,en;q=0.9",
                "If-Match": "*",
            },
        )
        verification_url = response_data["verification_url"]
        user_code = response_data["user_code"]

        self.to_screen(
            f"To give yt-dlp access to your account, go to  {verification_url}  and enter code  {user_code}"
        )
        self.to_screen(f"Sleeping for {response_data['interval']}")
        time.sleep(response_data["interval"])
        while True:
            response = _execute_request(
                "https://oauth2.googleapis.com/token",
                "POST",
                headers={"Content-Type": "application/json"},
                data={
                    "client_id": _CLIENT_ID,
                    "client_secret": _CLIENT_SECRET,
                    "device_code": response_data["device_code"],
                    "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                },
            )
            token_response = json.loads(response.read())
            self.to_screen("\n\n")

            error = traverse_obj(token_response, "error")
            self.to_screen(token_response)
            if error:
                if error == "authorization_pending":
                    time.sleep(response_data["interval"])
                    continue
                elif error == "expired_token":
                    self.report_warning(
                        "The device code has expired, restarting authorization flow"
                    )
                    return self.authorize()
                else:
                    raise ExtractorError(f"Unhandled OAuth2 Error: {error}")

            self.to_screen("Authorization successful")
            return {
                "access_token": token_response["access_token"],
                "expires": datetime.datetime.now(datetime.timezone.utc).timestamp()
                + token_response["expires_in"],
                "refresh_token": token_response["refresh_token"],
                "token_type": token_response["token_type"],
            }

        """self.access_token = response_data['access_token']
        self.refresh_token = response_data['refresh_token']
        self.expires = start_time + response_data['expires_in']
        self.cache_tokens()"""

    def __authorize(self):
        code_response = self._download_json(
            "https://www.youtube.com/o/oauth2/device/code",
            video_id="oauth2",
            note="Initializing OAuth2 Authorization Flow",
            data=json.dumps(
                {
                    "client_id": _CLIENT_ID,
                    "scope": _SCOPES,
                    "device_id": uuid.uuid4().hex,
                    "device_model": "ytlr::",
                }
            ).encode(),
            headers={"Content-Type": "application/json", "__youtube_oauth__": True},
        )

        verification_url = code_response["verification_url"]
        user_code = code_response["user_code"]
        self.to_screen(
            f"To give yt-dlp access to your account, go to  {verification_url}  and enter code  {user_code}"
        )

        while True:
            token_response = self._download_json(
                "https://www.youtube.com/o/oauth2/token",
                video_id="oauth2",
                note=False,
                data=json.dumps(
                    {
                        "client_id": _CLIENT_ID,
                        "client_secret": _CLIENT_SECRET,
                        "code": code_response["device_code"],
                        "grant_type": "http://oauth.net/grant_type/device/1.0",
                    }
                ).encode(),
                headers={"Content-Type": "application/json", "__youtube_oauth__": True},
            )

            error = traverse_obj(token_response, "error")
            if error:
                if error == "authorization_pending":
                    time.sleep(code_response["interval"])
                    continue
                elif error == "expired_token":
                    self.report_warning(
                        "The device code has expired, restarting authorization flow"
                    )
                    return self.authorize()
                else:
                    raise ExtractorError(f"Unhandled OAuth2 Error: {error}")

            self.to_screen("Authorization successful")
            return {
                "access_token": token_response["access_token"],
                "expires": datetime.datetime.now(datetime.timezone.utc).timestamp()
                + token_response["expires_in"],
                "refresh_token": token_response["refresh_token"],
                "token_type": token_response["token_type"],
            }


for _, ie in YOUTUBE_IES:

    class _YouTubeOAuth(ie, YouTubeOAuth2Handler, plugin_name="oauth2"):
        _NETRC_MACHINE = "youtube"
        _use_oauth2 = False

        # Remove any default *_creator clients as they do not support oauth
        _OAUTH2_UNSUPPORTED_CLIENTS = ("web_creator", "android_creator", "ios_creator")
        # Additional clients to add when using oauth
        _OAUTH2_CLIENTS = ("mweb",)

        def _perform_login(self, username, password):
            if username == "oauth2":
                self._use_oauth2 = True
                self.initialize_oauth()
                self._DEFAULT_CLIENTS = (
                    tuple(
                        c
                        for c in getattr(self, "_DEFAULT_CLIENTS", [])
                        if c not in self._OAUTH2_UNSUPPORTED_CLIENTS
                    )
                    + self._OAUTH2_CLIENTS
                )
                return

            return super()._perform_login(username, password)

        def _create_request(self, *args, **kwargs):
            request = super()._create_request(*args, **kwargs)
            if "__youtube_oauth__" in request.headers:
                request.headers.pop("__youtube_oauth__")
            elif self._use_oauth2:
                self.handle_oauth(request)
            return request

        @property
        def is_authenticated(self):
            if self._use_oauth2:
                token_data = self.get_token()
                return token_data and self.validate_token_data(token_data)
            return super().is_authenticated
