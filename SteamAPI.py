import requests
import base64
import math
import random
import json
import re
from threading import Timer
import pickle
import os
import sys
from Crypto.PublicKey import RSA
from Crypto.Cipher import PKCS1_v1_5
from pyee import EventEmitter
import SteamID

import logging
logging.basicConfig()
logger = logging.getLogger(__name__)


def save_cookies(session, filename):
    with open(filename, 'w') as f:
        f.truncate()
        pickle.dump(session.cookies._cookies, f)


def load_cookies(session, filename):
    if not os.path.isfile(filename):
        return False

    with open(filename) as f:
        cookies = pickle.load(f)
        if cookies:
            jar = requests.cookies.RequestsCookieJar()
            jar._cookies = cookies
            session.cookies = jar
        else:
            return False

    return True


def generateSessionID():
    return int(math.floor(random.random() * 1000000000))


class LoginStatus:
    Waiting, LoginFailed, LoginSuccessful, SteamGuard, TwoFactor, Captcha = range(
        6)


class ChatState:
    Offline, LoggingOn, LogOnFailed, LoggedOn = range(4)


class UserStatus:
    Offline = 0
    Online = 1
    Busy = 2
    Away = 3
    Snooze = 4
    LookingToTrade = 5
    LookingToPlay = 6
    Max = 7


class PersonaStateFlag:
    HasRichPresence = 1
    InJoinableGame = 2

    OnlineUsingWeb = 256
    OnlineUsingMobile = 512
    OnlineUsingBigPicture = 1024


class SteamAPI:
    def __init__(self):
        self.oauth_client_id = "DE45CD61"
        self._session = requests.Session()
        self._captchaGid = -1
        self.chatState = ChatState.Offline
        self.event = EventEmitter()

        self._session.cookies.set("Steam_Language", "english")
        self._session.cookies.set("timezoneOffset", "0,0")
        self._session.cookies.set("mobileClientVersion", "0 (2.1.3)")
        self._session.cookies.set("mobileClient", "android")

        self.jarLoaded = False

        self._timers = []
        self._cache = {}

        self.chatFriends = {}

        self._mobileHeaders = {
            "X-Requested-With": "com.valvesoftware.android.steam.community",
            "referer": "https://steamcommunity.com/mobilelogin?oauth_client_id=DE45CD61&oauth_scope=read_profile%20write_profile%20read_client%20write_client",
            "user-agent": "Mozilla/5.0 (Linux; U; Android 4.1.1; en-us; Google Nexus 4 - 4.1.1 - API 16 - 768x1280 Build/JRO03S) AppleWebKit/534.30 (KHTML, like Gecko) Version/4.0 Mobile Safari/534.30",
            "accept": "text/javascript, text/html, application/xml, text/xml, */*"
        }

    def _checkHttpError(self, response):
        if response.status_code >= 300 and response.status_code <= 399 and "/login" in response.headers["location"]:
            return True

        if response.status_code >= 400:
            response.raise_for_status()
            return True

        return False

    def loadJar(self, jar):
        if load_cookies(self._session, jar):
            self.jarLoaded = True

    def login(self, details, cookie_file=None):
        if cookie_file:
            self.loadJar(cookie_file)

        rsakey = self._session.post("https://steamcommunity.com/login/getrsakey/", data={
                                    "username": details["accountName"]}, headers=self._mobileHeaders)

        if rsakey.status_code != requests.codes.ok:
            rsakey.raise_for_status()
            return None, None

        mod = long(rsakey.json()["publickey_mod"], 16)
        exp = long(rsakey.json()["publickey_exp"], 16)
        rsa_key = RSA.construct((mod, exp))
        rsa = PKCS1_v1_5.PKCS115_Cipher(rsa_key)

        form = {
            "captcha_text": details.get("captcha", ""),
            "captchagid": self._captchaGid,
            "emailauth": details.get('steamguard', ""),
            "emailsteamid": "",
            "password": base64.b64encode(rsa.encrypt(details["password"])),
            "remember_login": "true",
            "rsatimestamp": rsakey.json()["timestamp"],
            "twofactorcode": details.get('two-factor', ""),
            "username": details['accountName'],
            "oauth_client_id": "DE45CD61",
            "oauth_scope": "read_profile write_profile read_client write_client",
            "loginfriendlyname": "#login_emailauth_friendlyname_mobile"
        }

        dologin = self._session.post(
            "https://steamcommunity.com/login/dologin/", data=form, headers=self._mobileHeaders).json()

        if not dologin["success"] and dologin.get("emailauth_needed"):
            self._cache = details
            return LoginStatus.SteamGuard
        elif not dologin["success"] and dologin.get("requires_twofactor"):
            self._cache = details
            return LoginStatus.TwoFactor
        elif not dologin["success"] and dologin.get("captcha_needed"):
            self._cache = details
            print "Captcha URL: https://steamcommunity.com/public/captcha.php?gid=", dologin["captcha_gid"]

            return LoginStatus.Captcha
        elif not dologin["success"]:
            raise Exception(dologin.get("message", "Unknown error"))
        else:
            sessionID = generateSessionID()
            oAuth = json.loads(dologin["oauth"])
            self._session.cookies.set("sessionid", str(sessionID))

            self.steamID = oAuth["steamid"]
            self.oAuthToken = oAuth["oauth_token"]

            self._cache = {}
            steamguard = self._session.cookies.get(
                "steamMachineAuth" + self.steamID, '')

            if cookie_file:
                save_cookies(self._session, cookie_file)
            return LoginStatus.LoginSuccessful

        self._cache = details
        return LoginStatus.LoginFailed

    def retry(self, details):
        deets = self._cache.copy()
        deets.update(details)
        return self.login(deets)

    def getWebApiOauthToken(self):
        resp = self._session.get("https://steamcommunity.com/chat")
        if self._checkHttpError(resp):
            return ("HTTP Error", None)

        token = re.compile(ur'"([0-9a-f]{32})"')
        matches = token.search(resp.text)
        if matches:
            self._initialLoadFriends(resp.text)
            return (None, matches.group().replace('"', ''))

        return ("Malformed Response", None)

    def _initialLoadFriends(self, resp):
        friends_json = re.compile(ur', (\[.*\]), ')
        matches = friends_json.search(resp)
        if matches:
            res = json.loads(matches.groups()[0])
            for friend in res:
                persona = {
                    "steamID": SteamID.SteamID(friend['m_ulSteamID']),
                    "personaName": friend['m_strName'],
                    "personaState": friend['m_ePersonaState'],
                    "personaStateFlags": friend.get('m_nPersonaStateFlags', 0),
                    "avatarHash": friend['m_strAvatarHash'],
                    "inGame": friend.get('m_bInGame', False),
                    "inGameAppID": friend.get('m_nInGameAppID', None),
                    "inGameName": friend.get('m_strInGameName', None)
                }
                self.chatFriends[str(persona["steamID"])] = persona

    def chatLogon(self, interval=500, uiMode="web", cookie_file=None):
        if cookie_file:
            self.loadJar(cookie_file)

        if self.chatState == ChatState.LoggingOn or self.chatState == ChatState.LoggedOn:
            return

        logger.info("Requesting chat WebAPI token")
        self.chatState = ChatState.LoggingOn

        err, token = self.getWebApiOauthToken()
        if err:
            logger.error("Cannot get oauth token: %s", err)
            self.chatState = ChatState.LogOnFailed
            timer = Timer(5.0, self.chatLogon)
            timer.daemon = True
            timer.start()
            return None

        login = self._session.post(
            "https://api.steampowered.com/ISteamWebUserPresenceOAuth/Logon/v1", data={"ui_mode": uiMode, "access_token": token})

        if login.status_code != 200:
            logger.error("Error logging into webchat (%s)", login.status_code)
            timer = Timer(5.0, self.chatLogon)
            timer.daemon = True
            timer.start()
            return None

        login_data = login.json()

        if login_data["error"] != "OK":
            logger.error("Error logging into webchat: %s", login_data["error"])
            timer = Timer(5.0, self.chatLogon)
            timer.daemon = True
            timer.start()
            return None

        self._chat = {
            "umqid": login_data["umqid"],
            "message": login_data["message"],
            "accessToken": token,
            "interval": interval
        }

        # self.friends = self._loadFriends()
        # ids = [friend["steamid"] for friend in self.friends]
        # for id_ in ids:
        #     self._chatUpdatePersona(SteamID.SteamID(id_))

        if cookie_file:
            save_cookies(self._session, cookie_file)

        self.chatState = ChatState.LoggedOn
        self._chatPoll()

    def chatMessage(self, recipient, text, type_="saytext"):
        if self.chatState != ChatState.LoggedOn:
            raise Exception(
                "Chat must be logged on before messages can be sent")

        if not isinstance(recipient, SteamID.SteamID):
            recipient = SteamID.SteamID(recipient)

        form = {
            "access_token": self._chat["accessToken"],
            "steamid_dst": recipient.SteamID64,
            "text": text,
            "type": type_,
            "umqid": self._chat["umqid"]
        }

        message = self._session.post(
            "https://api.steampowered.com/ISteamWebUserPresenceOAuth/Message/v1", data=form)

    def chatLogoff(self):
        logoff = self._session.post("https://api.steampowered.com/ISteamWebUserPresenceOAuth/Logoff/v1", data={
            "access_token": self._chat["accessToken"],
            "umqid": self._chat["umqid"]
        })

        if logoff.status_code != 200:
            logger.error("Error logging off of chat: %s", logoff.status_code)
            timer = Timer(1.0, self.chatLogoff)
            timer.daemon = True
            timer.start()
        else:
            self._chat = {}
            self.chatFriends = {}
            self.chatState = ChatState.Offline
            self.Exit()

    def _chatPoll(self):
        form = {
            "umqid": self._chat["umqid"],
            "message": self._chat["message"],
            "pollid": 1,
            "sectimeout": 20,
            "secidletime": 0,
            "use_accountids": 1,
            "access_token": self._chat["accessToken"]
        }

        response = self._session.post(
            "https://api.steampowered.com/ISteamWebUserPresenceOAuth/Poll/v1", data=form)

        if self.chatState == ChatState.Offline:
            return None

        self._chat["timer"] = Timer(
            self._chat['interval'] / 1000.0, self._chatPoll, ())
        self._chat["timer"].daemon = True
        self._chat["timer"].start()

        if response.status_code != 200:
            logger.error("Error in chat poll: %s", response.status_code)
            response.raise_for_status()
            return None

        body = response.json()

        # if body["error"] != "OK":
        #     print "Error in chat poll: ", body["error"]

        self._chat['message'] = body.get("messagelast", "")

        for message in body.get("messages", []):
            sender = SteamID.SteamID()
            sender.universe = SteamID.Universe.PUBLIC
            sender.type = SteamID.Type.INDIVIDUAL
            sender.instance = SteamID.Instance.DESKTOP
            sender.accountid = message['accountid_from']

            type_ = message["type"]
            if type_ == "personastate":
                self._chatUpdatePersona(sender)
            elif type_ == "saytext":
                self.event.emit('chatMessage', str(sender), message["text"])
            elif type_ == "typing":
                self.event.emit('chatTyping', sender)
            else:
                logger.warning("Unhandled message type: %s", type_)

    def Exit(self):
        sys.exit()

    def avatarUrl(self, hashed):
        tag = hashed[:1]
        url = "http://cdn.akamai.steamstatic.com/steamcommunity/public/images/avatars/{tag}/{hash}_full.jpg".format(
            tag=tag, hash=hashed)

        if hashed == ("0" * 40):
            return "http://cdn.akamai.steamstatic.com/steamcommunity/public/images/avatars/fe/fef49e7fa7e1997310d705b2a6158ff8dc1cdfeb_full.jpg"
        return url

    def _loadFriends(self):
        form = {
            "access_token": self.oAuthToken,
            "steamid": self.steamID
        }

        response = self._session.get(
            "https://api.steampowered.com/ISteamUserOAuth/GetFriendList/v0001", params=form, headers=self._mobileHeaders)

        if response.status_code != 200:
            logger.error("Load friends error: %s", response.status_code)
            timer = Timer(2.0, self._loadFriends)
            timer.daemon = True
            timer.start()
            return None

        body = response.json()
        if "friends" in body:
            return body["friends"]

        return None

    def _chatUpdatePersona(self, steamID):
        accnum = steamID.accountid
        response = self._session.get(
            "https://steamcommunity.com/chat/friendstate/" + str(accnum))

        if response.status_code != 200:
            logger.error("Chat update persona error: %s", response.status_code)
            timer = Timer(2.0, self._chatUpdatePersona, (steamID))
            timer.daemon = True
            timer.start()
            return None

        body = response.json()

        if str(steamID) in self.chatFriends:
            old_persona = self.chatFriends[str(steamID)]
            steamID = old_persona["steamID"]
        else:
            old_persona = {}

        persona = {
            "steamID": steamID,
            "personaName": body['m_strName'],
            "personaState": body['m_ePersonaState'],
            "personaStateFlags": body.get('m_nPersonaStateFlags', 0),
            "avatarHash": body['m_strAvatarHash'],
            "inGame": body.get('m_bInGame', False),
            "inGameAppID": body.get('m_nInGameAppID', None),
            "inGameName": body.get('m_strInGameName', None)
        }

        diff = {}

        for key in persona.keys():
            if key in old_persona:
                if old_persona[key] != persona[key]:
                    diff[key] = persona[key]
            else:
                diff[key] = persona[key]

        self.event.emit(
            'chatPersonaState', steamID, persona, old_persona, diff)
        self.chatFriends[str(steamID)] = persona
