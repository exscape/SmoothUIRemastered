"""The update check: whether a newer Smooth UI Tools release exists on GitHub, and new Smooth UI release
manifests, which it downloads into data\\manifests\\ (release_manifest_paths reads them from there at once).

One request to GitHub's API lists every release with its assets (unauthenticated: 60 requests an hour per
address, so never more than one per check, and none within RECENT of the last one: the window keeps
its time in the settings). Two kinds of release count:
- the tools: tag tools-vX.Y.Z, published (no draft, no pre-release), with one .zip asset;
- the manifests: tag "manifests", one long-lived release holding the manifests of every Smooth UI release
  (<version>.json), so that a Smooth UI release reaches the merger without a tools release.
Assets are downloaded from their download links (which don't count against the limit), and each must match
the SHA-256 GitHub lists for it.

Every failure (offline, the limit reached, an unexpected answer) is one log line: check() never raises.
HTTPS only, except to this computer (the test variable, UPDATE_URL_ENV, may name a local test server).
"""

from __future__ import annotations

import hashlib
import http.client
import json
import logging
import os
import re
import ssl
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime
from functools import cache
from pathlib import Path
from urllib.parse import urlsplit

import truststore

from ..common import SmoothUIError, fail, normalize_version
from ..files import delete_file, make_dirs, read_file
from ..manifests import is_dev_version, version_key
from ..paths import MANIFEST_DIR
from . import RELEASES_URL, UPDATE_URL_ENV, merger_version

log = logging.getLogger(__name__)

# GitHub's maximum; a manifests release beyond the first page is fetched by its tag
PER_PAGE = 100
TIMEOUT = 10
# When the game waits for the check (the launcher opened the merger)
QUICK_TIMEOUT = 5
# No new check within this long of the last one (and no offer: the next start after it makes it again);
# while the test variable is set, TEST_RECENT, so that tests can start the merger again at once
RECENT = 10 * 60     # s
TEST_RECENT = 5
# Where downloaded manifests go, in the data folder
MANIFESTS_DIR = "manifests"

TOOLS_TAG = re.compile(r"tools-v([0-9]+)\.([0-9]+)\.([0-9]+)")
MANIFESTS_TAG = "manifests"
DIGEST = re.compile(r"sha256:([0-9a-f]{64})")
LOCAL_HOSTS = ("127.0.0.1", "localhost")
CHUNK = 64 * 1024


@dataclass(slots=True)
class Asset:
    name: str
    url: str
    size: int
    sha256: str     # hex


@dataclass(slots=True)
class ToolsUpdate:
    version: str                    # X.Y.Z
    notes: list[tuple[str, str]]    # (version, release notes in Markdown) of each newer release, newest first
    asset: Asset                    # the zip


@dataclass(slots=True)
class Found:
    update: ToolsUpdate | None      # the newest tools release newer than this one
    downloaded: list[str]           # the versions of the manifests downloaded by this check


def releases_url() -> str:
    return os.environ.get(UPDATE_URL_ENV) or RELEASES_URL


def check(data_dir: Path, *, timeout: float = TIMEOUT, last_check: float | None = None) -> Found | None:
    """Look for a newer tools release, and download new manifests into data_dir\\manifests\\. Within
    RECENT of last_check (the last successful check, Unix time; None: never, or not to be taken into account),
    nothing is asked or found. None if the check failed or was skipped (logged)."""
    url = releases_url()
    if last_check is not None and _recent(time.time() - last_check):
        return None
    try:
        current = merger_version()
        releases = _get_json(_with_query(url, f"per_page={PER_PAGE}"), timeout)
        update = tools_update(releases, current)
        manifests = _manifests_release(releases)
        if manifests is None and len(releases) >= PER_PAGE:
            try:
                manifests = _get_json(f"{url.split('?')[0].rstrip('/')}/tags/{MANIFESTS_TAG}", timeout)
            except _NotFound:
                pass
        assets = manifest_assets(manifests) if manifests else []
    except (SmoothUIError, _NotFound) as e:
        log.info(f"Update check failed: {e}")
        return None
    except (KeyError, TypeError, AttributeError, ValueError) as e:
        log.info(f"Update check failed: the release list from {url} isn't as expected ({e!r})")
        return None
    if update:
        log.info(f"Update check: Smooth UI Tools v{update.version} is available (this is v{current})")
    else:
        log.info(f"Update check: no newer Smooth UI Tools (this is v{current})")
    folder = Path(data_dir) / MANIFESTS_DIR
    downloaded = download_manifests(manifests_to_download(assets, (MANIFEST_DIR, folder)), folder, timeout)
    return Found(update, downloaded)


def tools_update(releases: list, current: str) -> ToolsUpdate | None:
    """The newest tools release newer than current (X.Y.Z), with the notes of every newer one"""
    newer = []
    for release in releases:
        match = TOOLS_TAG.fullmatch(release["tag_name"])
        if not match or release["draft"] or release["prerelease"]:
            continue
        version = tuple(int(number) for number in match.groups())
        if version <= _version_tuple(current):
            continue
        zips = [asset for asset in release["assets"] if asset["name"].lower().endswith(".zip")]
        if len(zips) != 1 or (asset := _asset(zips[0])) is None:
            log.info(f"Update check: {release['tag_name']} is left out: it doesn't have exactly one zip with a "
                     "SHA-256")
            continue
        newer.append((version, release.get("body") or "", asset))
    if not newer:
        return None
    newer.sort(key=lambda item: item[0], reverse=True)
    version, _, asset = newer[0]
    return ToolsUpdate(_version_text(version), [(_version_text(v), notes) for v, notes, _ in newer], asset)


def manifest_assets(release: dict) -> list[Asset]:
    """The manifests release's assets that are manifests of a Smooth UI release: <version>.json, not of a
    dev build, with a SHA-256"""
    found = []
    for asset in release["assets"]:
        name = asset["name"]
        version = name.removesuffix(".json")
        if (name.endswith(".json") and normalize_version(version) == version and version_key(version) is not None
                and not is_dev_version(version) and (checked := _asset(asset)) is not None):
            found.append(checked)
    return found


def manifests_to_download(assets: list[Asset], folders) -> list[Asset]:
    """The assets that no file in folders is a copy of (the same name and SHA-256): new manifests, and
    corrected ones (uploaded again under the same name)"""
    def have(asset):
        return any(_sha256(Path(folder) / asset.name) == asset.sha256 for folder in folders)
    return [asset for asset in assets if not have(asset)]


def download_manifests(assets: list[Asset], folder: Path, timeout: float = TIMEOUT) -> list[str]:
    """Download manifests into folder; the versions downloaded. Each goes through a temporary name, and is
    renamed only once its SHA-256 matches and it reads as a manifest. One that fails is deleted (and logged),
    and downloaded again by the next check."""
    downloaded = []
    for asset in assets:
        target = Path(folder) / asset.name
        part = target.with_name(f"{asset.name}.part")
        try:
            make_dirs(folder)
            download(asset, part, timeout)
            try:
                manifest = json.loads(read_file(part).decode("utf-8"))
            except ValueError as e:
                fail(f"it can't be read: {e}")
            if not isinstance(manifest, dict):
                fail("it isn't a manifest")
            os.replace(part, target)
        except (SmoothUIError, OSError) as e:
            log.warning(f"Update check: the manifest {asset.name} can't be downloaded: {e}")
            _delete_quietly(part)
            continue
        downloaded.append(asset.name.removesuffix(".json"))
        log.info(f"Update check: downloaded the manifest of Smooth UI {downloaded[-1]}")
    return downloaded


def download(asset: Asset, path: Path, timeout: float = TIMEOUT, progress=None):
    """Download an asset to path (replacing any file there), checking its size and SHA-256. progress, if
    given, is called with (bytes so far, asset.size) after each chunk. Fails (SmoothUIError) if anything
    goes wrong, and then deletes what it wrote."""
    sha256 = hashlib.sha256()
    done = 0
    try:
        with _open(asset.url, timeout, "application/octet-stream") as response, open(path, "wb") as f:
            while chunk := response.read(CHUNK):
                done += len(chunk)
                if done > asset.size:
                    fail(f"{asset.name} is larger than the {asset.size} bytes GitHub lists")
                sha256.update(chunk)
                f.write(chunk)
                if progress:
                    progress(done, asset.size)
        if done != asset.size:
            fail(f"{asset.name} ended after {done} of its {asset.size} bytes")
        if sha256.hexdigest() != asset.sha256:
            fail(f"{asset.name} doesn't have the SHA-256 GitHub lists for it")
    except (OSError, http.client.HTTPException) as e:
        _delete_quietly(path)
        fail(_describe(e, asset.url, timeout))
    except SmoothUIError:
        _delete_quietly(path)
        raise


def allowed_url(url: str) -> bool:
    """HTTPS, or HTTP to this computer (a test server)"""
    parts = urlsplit(url)
    return parts.scheme == "https" or (parts.scheme == "http" and parts.hostname in LOCAL_HOSTS)


class _NotFound(Exception):
    """A 404 from the server (for the manifests release: there is none)"""


class _HttpsRedirects(urllib.request.HTTPRedirectHandler):
    """Follows redirects only to allowed URLs (GitHub's download links redirect to another host), adding each
    to the first request's list of URLs (see _open)"""
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not allowed_url(newurl):
            raise urllib.error.URLError(f"redirected to {newurl}, which isn't HTTPS")
        new = super().redirect_request(req, fp, code, msg, headers, newurl)
        if new is not None:
            new.urls = req.urls
            new.urls.append(newurl)
        return new


@cache
def _opener():
    # Windows itself checks the certificates (truststore), as for its own programs: it then downloads a root
    # certificate it doesn't have yet. Python's own check would see only those already in Windows' store, and
    # a fresh Windows has few (GitHub's API and its download host need two different ones).
    context = truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    return urllib.request.build_opener(_HttpsRedirects, urllib.request.HTTPSHandler(context=context))


def _open(url, timeout, accept):
    if not allowed_url(url):
        fail(f"{url} isn't HTTPS")
    headers = {"User-Agent": f"SmoothUITools/{merger_version()}", "Accept": accept,
               "X-GitHub-Api-Version": "2022-11-28"}
    request = urllib.request.Request(url, headers=headers)
    request.urls = [url]    # then where it was redirected to
    try:
        return _opener().open(request, timeout=timeout)
    except (OSError, http.client.HTTPException) as e:
        e.failed_url = request.urls[-1]     # for _describe: after a redirect, the other host failed
        raise


def _get_json(url, timeout):
    try:
        with _open(url, timeout, "application/vnd.github+json") as response:
            data = response.read()
    except urllib.error.HTTPError as e:
        if e.code == 404:
            raise _NotFound(f"{url} wasn't found") from e
        fail(_describe(e, url, timeout))
    except (OSError, http.client.HTTPException) as e:
        fail(_describe(e, url, timeout))
    try:
        return json.loads(data)
    except ValueError as e:
        fail(f"the answer from {url} isn't JSON: {e}")


def _describe(e, url, timeout) -> str:
    url = getattr(e, "failed_url", url)
    host = urlsplit(url).hostname
    if isinstance(e, urllib.error.HTTPError):
        if e.code in (403, 429) and e.headers.get("X-RateLimit-Remaining") == "0":
            reset = e.headers.get("X-RateLimit-Reset", "")
            until = (f", until {datetime.fromtimestamp(int(reset)).strftime('%H:%M')}" if reset.isdigit() else "")
            return f"GitHub's limit of checks from this address (60 an hour) is reached{until}"
        return f"{host} answered {e.code} {e.reason} for {url}"
    if isinstance(e, TimeoutError) or isinstance(getattr(e, "reason", None), TimeoutError):
        return f"no answer from {host} within {timeout} s"
    if isinstance(e, urllib.error.URLError):
        # A string: refused before connecting (see _HttpsRedirects)
        return f"{url}: {e.reason}" if isinstance(e.reason, str) else f"can't reach {host}: {e.reason}"
    return f"{url}: {e}"


def _manifests_release(releases):
    return next((release for release in releases if release["tag_name"] == MANIFESTS_TAG), None)


def _asset(asset: dict) -> Asset | None:
    """An asset as GitHub lists it; None if it has no SHA-256 (uploaded before GitHub added them)"""
    match = DIGEST.fullmatch(asset.get("digest") or "")
    if not match:
        return None
    return Asset(asset["name"], asset["browser_download_url"], int(asset["size"]), match[1])


def _recent(age: float) -> bool:
    """Whether a check this long ago (s) is recent enough to skip a new one (logged); one in the future isn't"""
    limit = TEST_RECENT if os.environ.get(UPDATE_URL_ENV) else RECENT
    if not 0 <= age < limit:
        return False
    ago = f"{int(age // 60)} min" if age >= 60 else f"{int(age)} s"
    log.info(f"Update check: skipped (the last one was {ago} ago)")
    return True


def _sha256(path: Path) -> str | None:
    try:
        data = read_file(path)
    except SmoothUIError:
        return None
    return hashlib.sha256(data).hexdigest() if data is not None else None


def _delete_quietly(path):
    try:
        delete_file(path)
    except SmoothUIError:
        pass


def _with_query(url, query):
    return f"{url}{'&' if '?' in url else '?'}{query}"


def _version_tuple(version: str) -> tuple[int, ...]:
    return tuple(int(number) for number in version.split("."))


def _version_text(version: tuple[int, ...]) -> str:
    return ".".join(str(number) for number in version)
